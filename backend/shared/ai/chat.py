"""Pinned LangChain adapter: the application owns tool execution and retries."""
from langchain_google_genai import ChatGoogleGenerativeAI
from google.genai import types
from langchain_core.messages import AIMessage, ToolMessage


class VertexChatModel(ChatGoogleGenerativeAI):
    def _prepare_request(self, messages, **kwargs):
        request = super()._prepare_request(messages, **kwargs)
        # 4.3.7 parses provider IDs but omits them from replayed function parts.
        # Restore only the exact IDs from paired LangChain history; never guess
        # identity from a function name (the same tool can be called repeatedly).
        calls, responses, pending = [], [], {}
        for message in messages:
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    identity = call.get("id")
                    if not identity or identity in pending:
                        raise ValueError("Google tool history has an ambiguous call identity")
                    calls.append(call)
                    pending[identity] = call
            elif isinstance(message, ToolMessage):
                call = pending.pop(message.tool_call_id, None)
                if call is None:
                    raise ValueError("Google tool response has no matching call")
                responses.append((message.tool_call_id, call["name"]))
        parts = [part for content in request["contents"] for part in content.parts]
        formatted_calls = [part.function_call for part in parts if part.function_call]
        formatted_responses = [part.function_response for part in parts if part.function_response]
        if pending or len(calls) != len(formatted_calls) or len(responses) != len(formatted_responses):
            raise ValueError("Google tool history is incomplete or was changed during conversion")
        for original, formatted in zip(calls, formatted_calls, strict=True):
            if formatted.name != original["name"] or dict(formatted.args or {}) != original["args"]:
                raise ValueError("Google tool call changed during conversion")
            formatted.id = original["id"]
        for (identity, name), formatted in zip(responses, formatted_responses, strict=True):
            if formatted.name != name:
                raise ValueError("Google tool response changed during conversion")
            formatted.id = identity
        outstanding = []
        for content in request["contents"]:
            outstanding.extend(part.function_call.id for part in content.parts if part.function_call)
            slots = [index for index, part in enumerate(content.parts) if part.function_response]
            if slots:
                ordered = outstanding[:len(slots)]
                available = {content.parts[index].function_response.id: content.parts[index] for index in slots}
                if set(ordered) != set(available) or len(available) != len(slots):
                    raise ValueError("Google tool turn does not match its preceding calls")
                for index, identity in zip(slots, ordered, strict=True):
                    content.parts[index] = available[identity]
                outstanding = outstanding[len(slots):]
        if outstanding:
            raise ValueError("Google tool turn is incomplete")
        return request

    def _build_request_config(self, *args, **kwargs):
        # This integration seam is verified against langchain-google-genai 4.3.7.
        config = super()._build_request_config(*args, **kwargs)
        # Gemini 4 rejects candidate_count and penalties, even a candidate count
        # of one supplied by LangChain's generic defaults. Sampling is fixed.
        config.candidate_count = None
        config.frequency_penalty = config.presence_penalty = None
        config.temperature = config.top_p = config.top_k = None
        config.automatic_continuation = False
        config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)
        if config.http_options is None:
            config.http_options = types.HttpOptions()
        config.http_options.retry_options = types.HttpRetryOptions(attempts=1)
        return config
