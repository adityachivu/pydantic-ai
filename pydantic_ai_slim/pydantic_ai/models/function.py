from __future__ import annotations as _annotations

import inspect
import re
from collections.abc import AsyncIterator, Awaitable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from itertools import chain
from typing import Callable, Union

from typing_extensions import TypeAlias, assert_never, overload

from pydantic_ai.profiles import ModelProfileSpec

from .. import _utils, usage
from .._utils import PeekableAsyncStream
from ..messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponseStreamEvent,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)
from ..settings import ModelSettings
from ..tools import ToolDefinition
from . import Model, ModelRequestParameters, StreamedResponse


@dataclass(init=False)
class FunctionModel(Model):
    """A model controlled by a local function.

    Apart from `__init__`, all methods are private or match those of the base class.
    """

    function: FunctionDef | None = None
    stream_function: StreamFunctionDef | None = None

    _model_name: str = field(repr=False)
    _system: str = field(default='function', repr=False)

    @overload
    def __init__(
        self,
        function: FunctionDef,
        *,
        model_name: str | None = None,
        profile: ModelProfileSpec | None = None,
        settings: ModelSettings | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        stream_function: StreamFunctionDef,
        model_name: str | None = None,
        profile: ModelProfileSpec | None = None,
        settings: ModelSettings | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self,
        function: FunctionDef,
        *,
        stream_function: StreamFunctionDef,
        model_name: str | None = None,
        profile: ModelProfileSpec | None = None,
        settings: ModelSettings | None = None,
    ) -> None: ...

    def __init__(
        self,
        function: FunctionDef | None = None,
        *,
        stream_function: StreamFunctionDef | None = None,
        model_name: str | None = None,
        profile: ModelProfileSpec | None = None,
        settings: ModelSettings | None = None,
    ):
        """Initialize a `FunctionModel`.

        Either `function` or `stream_function` must be provided, providing both is allowed.

        Args:
            function: The function to call for non-streamed requests.
            stream_function: The function to call for streamed requests.
            model_name: The name of the model. If not provided, a name is generated from the function names.
            profile: The model profile to use.
            settings: Model-specific settings that will be used as defaults for this model.
        """
        if function is None and stream_function is None:
            raise TypeError('Either `function` or `stream_function` must be provided')

        self.function = function
        self.stream_function = stream_function

        function_name = self.function.__name__ if self.function is not None else ''
        stream_function_name = self.stream_function.__name__ if self.stream_function is not None else ''
        self._model_name = model_name or f'function:{function_name}:{stream_function_name}'

        super().__init__(settings=settings, profile=profile)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        agent_info = AgentInfo(
            model_request_parameters.function_tools,
            model_request_parameters.allow_text_output,
            model_request_parameters.output_tools,
            model_settings,
        )

        assert self.function is not None, 'FunctionModel must receive a `function` to support non-streamed requests'

        if inspect.iscoroutinefunction(self.function):
            response = await self.function(messages, agent_info)
        else:
            response_ = await _utils.run_in_executor(self.function, messages, agent_info)
            assert isinstance(response_, ModelResponse), response_
            response = response_
        response.model_name = self._model_name
        # Add usage data if not already present
        if not response.usage.has_values():  # pragma: no branch
            response.usage = _estimate_usage(chain(messages, [response]))
            response.usage.requests = 1
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> AsyncIterator[StreamedResponse]:
        agent_info = AgentInfo(
            model_request_parameters.function_tools,
            model_request_parameters.allow_text_output,
            model_request_parameters.output_tools,
            model_settings,
        )

        assert self.stream_function is not None, (
            'FunctionModel must receive a `stream_function` to support streamed requests'
        )

        response_stream = PeekableAsyncStream(self.stream_function(messages, agent_info))

        first = await response_stream.peek()
        if isinstance(first, _utils.Unset):
            raise ValueError('Stream function must return at least one item')

        yield FunctionStreamedResponse(_model_name=self._model_name, _iter=response_stream)

    @property
    def model_name(self) -> str:
        """The model name."""
        return self._model_name

    @property
    def system(self) -> str:
        """The system / model provider."""
        return self._system


@dataclass(frozen=True)
class AgentInfo:
    """Information about an agent.

    This is passed as the second to functions used within [`FunctionModel`][pydantic_ai.models.function.FunctionModel].
    """

    function_tools: list[ToolDefinition]
    """The function tools available on this agent.

    These are the tools registered via the [`tool`][pydantic_ai.Agent.tool] and
    [`tool_plain`][pydantic_ai.Agent.tool_plain] decorators.
    """
    allow_text_output: bool
    """Whether a plain text output is allowed."""
    output_tools: list[ToolDefinition]
    """The tools that can called to produce the final output of the run."""
    model_settings: ModelSettings | None
    """The model settings passed to the run call."""


@dataclass
class DeltaToolCall:
    """Incremental change to a tool call.

    Used to describe a chunk when streaming structured responses.
    """

    name: str | None = None
    """Incremental change to the name of the tool."""
    json_args: str | None = None
    """Incremental change to the arguments as JSON"""
    tool_call_id: str | None = None
    """Incremental change to the tool call ID."""


@dataclass
class DeltaThinkingPart:
    """Incremental change to a thinking part.

    Used to describe a chunk when streaming thinking responses.
    """

    content: str | None = None
    """Incremental change to the thinking content."""
    signature: str | None = None
    """Incremental change to the thinking signature."""


DeltaToolCalls: TypeAlias = dict[int, DeltaToolCall]
"""A mapping of tool call IDs to incremental changes."""

DeltaThinkingCalls: TypeAlias = dict[int, DeltaThinkingPart]
"""A mapping of thinking call IDs to incremental changes."""

# TODO: Change the signature to Callable[[list[ModelMessage], ModelSettings, ModelRequestParameters], ...]
FunctionDef: TypeAlias = Callable[[list[ModelMessage], AgentInfo], Union[ModelResponse, Awaitable[ModelResponse]]]
"""A function used to generate a non-streamed response."""

# TODO: Change signature as indicated above
StreamFunctionDef: TypeAlias = Callable[
    [list[ModelMessage], AgentInfo], AsyncIterator[Union[str, DeltaToolCalls, DeltaThinkingCalls]]
]
"""A function used to generate a streamed response.

While this is defined as having return type of `AsyncIterator[Union[str, DeltaToolCalls, DeltaThinkingCalls]]`, it should
really be considered as `Union[AsyncIterator[str], AsyncIterator[DeltaToolCalls], AsyncIterator[DeltaThinkingCalls]]`,

E.g. you need to yield all text, all `DeltaToolCalls`, or all `DeltaThinkingCalls`, not mix them.
"""


@dataclass
class FunctionStreamedResponse(StreamedResponse):
    """Implementation of `StreamedResponse` for [FunctionModel][pydantic_ai.models.function.FunctionModel]."""

    _model_name: str
    _iter: AsyncIterator[str | DeltaToolCalls | DeltaThinkingCalls]
    _timestamp: datetime = field(default_factory=_utils.now_utc)

    def __post_init__(self):
        self._usage += _estimate_usage([])

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        async for item in self._iter:
            if isinstance(item, str):
                response_tokens = _estimate_string_tokens(item)
                self._usage += usage.Usage(response_tokens=response_tokens, total_tokens=response_tokens)
                maybe_event = self._parts_manager.handle_text_delta(vendor_part_id='content', content=item)
                if maybe_event is not None:  # pragma: no branch
                    yield maybe_event
            elif isinstance(item, dict) and item:
                for dtc_index, delta in item.items():
                    if isinstance(delta, DeltaThinkingPart):
                        if delta.content:  # pragma: no branch
                            response_tokens = _estimate_string_tokens(delta.content)
                            self._usage += usage.Usage(response_tokens=response_tokens, total_tokens=response_tokens)
                        yield self._parts_manager.handle_thinking_delta(
                            vendor_part_id=dtc_index,
                            content=delta.content,
                            signature=delta.signature,
                        )
                    elif isinstance(delta, DeltaToolCall):
                        if delta.json_args:
                            response_tokens = _estimate_string_tokens(delta.json_args)
                            self._usage += usage.Usage(response_tokens=response_tokens, total_tokens=response_tokens)
                        maybe_event = self._parts_manager.handle_tool_call_delta(
                            vendor_part_id=dtc_index,
                            tool_name=delta.name,
                            args=delta.json_args,
                            tool_call_id=delta.tool_call_id,
                        )
                        if maybe_event is not None:  # pragma: no branch
                            yield maybe_event
                    else:
                        assert_never(delta)

    @property
    def model_name(self) -> str:
        """Get the model name of the response."""
        return self._model_name

    @property
    def timestamp(self) -> datetime:
        """Get the timestamp of the response."""
        return self._timestamp


def _estimate_usage(messages: Iterable[ModelMessage]) -> usage.Usage:
    """Very rough guesstimate of the token usage associated with a series of messages.

    This is designed to be used solely to give plausible numbers for testing!
    """
    # there seem to be about 50 tokens of overhead for both Gemini and OpenAI calls, so add that here ¯\_(ツ)_/¯
    request_tokens = 50
    response_tokens = 0
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, (SystemPromptPart, UserPromptPart)):
                    request_tokens += _estimate_string_tokens(part.content)
                elif isinstance(part, ToolReturnPart):
                    request_tokens += _estimate_string_tokens(part.model_response_str())
                elif isinstance(part, RetryPromptPart):
                    request_tokens += _estimate_string_tokens(part.model_response())
                else:
                    assert_never(part)
        elif isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, TextPart):
                    response_tokens += _estimate_string_tokens(part.content)
                elif isinstance(part, ThinkingPart):
                    response_tokens += _estimate_string_tokens(part.content)
                elif isinstance(part, ToolCallPart):
                    response_tokens += 1 + _estimate_string_tokens(part.args_as_json_str())
                else:
                    assert_never(part)
        else:
            assert_never(message)
    return usage.Usage(
        request_tokens=request_tokens,
        response_tokens=response_tokens,
        total_tokens=request_tokens + response_tokens,
    )


def _estimate_string_tokens(content: str | Sequence[UserContent]) -> int:
    if not content:
        return 0

    if isinstance(content, str):
        return len(_TOKEN_SPLIT_RE.split(content.strip()))

    tokens = 0
    for part in content:
        if isinstance(part, str):
            tokens += len(_TOKEN_SPLIT_RE.split(part.strip()))
        elif isinstance(part, BinaryContent):
            tokens += len(part.data)
        # TODO(Marcelo): We need to study how we can estimate the tokens for AudioUrl or ImageUrl.

    return tokens


_TOKEN_SPLIT_RE = re.compile(r'[\s",.:]+')
