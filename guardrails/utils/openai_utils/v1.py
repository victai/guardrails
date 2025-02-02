import os
from typing import Any, AsyncIterable, Dict, Iterable, List, cast

import openai

from guardrails.utils.llm_response import LLMResponse
from guardrails.utils.openai_utils.base import BaseOpenAIClient
from guardrails.utils.openai_utils.streaming_utils import (
    num_tokens_from_messages,
    num_tokens_from_string,
)


def get_static_openai_create_func():
    if "OPENAI_API_KEY" not in os.environ:
        return None
    return openai.completions.create


def get_static_openai_chat_create_func():
    if "OPENAI_API_KEY" not in os.environ:
        return None
    return openai.chat.completions.create


def get_static_openai_acreate_func():
    return None


def get_static_openai_chat_acreate_func():
    return None


OpenAIServiceUnavailableError = openai.APIError


class OpenAIClientV1(BaseOpenAIClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = openai.Client(
            api_key=self.api_key,
            base_url=self.api_base,
        )

    def create_embedding(
        self,
        model: str,
        input: List[str],
    ) -> List[List[float]]:
        embeddings = self.client.embeddings.create(
            model=model,
            input=input,
        )
        return [r.embedding for r in embeddings.data]

    def create_completion(
        self, engine: str, prompt: str, *args, **kwargs
    ) -> LLMResponse:
        response = self.client.completions.create(
            model=engine, prompt=prompt, *args, **kwargs
        )

        return self.construct_nonchat_response(
            stream=kwargs.get("stream", False),
            openai_response=response,
            prompt=prompt,
            engine=engine,
        )

    def construct_nonchat_response(
        self,
        stream: bool,
        openai_response: Any,
        prompt: str,
        engine: str,
    ) -> LLMResponse:
        """Construct an LLMResponse from an OpenAI response.

        Splits execution based on whether the `stream` parameter is set
        in the kwargs.
        """
        if stream:
            # If stream is defined and set to True,
            # openai returns a generator object
            complete_output = ""
            openai_response = cast(Iterable[Dict[str, Any]], openai_response)
            for response in openai_response:
                complete_output += response["choices"][0]["text"]

            # Also, it no longer returns usage information
            # So manually count the tokens using tiktoken
            prompt_token_count = num_tokens_from_string(
                text=prompt,
                model_name=engine,
            )
            response_token_count = num_tokens_from_string(
                text=complete_output, model_name=engine
            )

            # Return the LLMResponse
            return LLMResponse(
                output=complete_output,
                prompt_token_count=prompt_token_count,
                response_token_count=response_token_count,
            )

        # If stream is not defined or is set to False,
        # return default behavior
        openai_response = cast(Dict[str, Any], openai_response)
        if not openai_response.choices:
            raise ValueError("No choices returned from OpenAI")
        if openai_response.usage is None:
            raise ValueError("No token counts returned from OpenAI")
        return LLMResponse(
            output=openai_response.choices[0].text,  # type: ignore
            prompt_token_count=openai_response.usage.prompt_tokens,  # type: ignore
            response_token_count=openai_response.usage.completion_tokens,  # noqa: E501 # type: ignore
        )

    def create_chat_completion(
        self, model: str, messages: List[Any], *args, **kwargs
    ) -> LLMResponse:
        response = self.client.chat.completions.create(
            model=model, messages=messages, *args, **kwargs
        )

        return self.construct_chat_response(
            stream=kwargs.get("stream", False),
            openai_response=response,
            prompt=messages,
            model=model,
        )

    def construct_chat_response(
        self,
        stream: bool,
        openai_response: Any,
        prompt: List[Any],
        model: str,
    ) -> LLMResponse:
        """Construct an LLMResponse from an OpenAI response.

        Splits execution based on whether the `stream` parameter is set
        in the kwargs.
        """
        if stream:
            # If stream is defined and set to True,
            # openai returns a generator object
            collected_messages = []
            openai_response = cast(Iterable[Dict[str, Any]], openai_response)
            for chunk in openai_response:
                chunk_message = chunk["choices"][0]["delta"]  # extract the message
                collected_messages.append(chunk_message)  # save the message

            complete_output = "".join(
                [msg.get("content", "") for msg in collected_messages]
            )

            # Also, it no longer returns usage information
            # So manually count the tokens using tiktoken
            prompt_token_count = num_tokens_from_messages(
                messages=prompt,
                model=model,
            )
            response_token_count = num_tokens_from_string(
                text=complete_output, model_name=model
            )

            # Return the LLMResponse
            return LLMResponse(
                output=complete_output,
                prompt_token_count=prompt_token_count,
                response_token_count=response_token_count,
            )

        # If stream is not defined or is set to False,
        # extract string from response
        openai_response = cast(Dict[str, Any], openai_response)
        if not openai_response.choices:
            raise ValueError("No choices returned from OpenAI")
        if not openai_response.choices[0].message.content:
            raise ValueError("No message returned from OpenAI")
        if openai_response.usage is None:
            raise ValueError("No token counts returned from OpenAI")

        if "function_call" in openai_response.choices[0].message:  # type: ignore
            output = openai_response.choices[
                0
            ].message.function_call.arguments  # noqa: E501 # type: ignore
        else:
            output = openai_response.choices[0].message.content  # type: ignore

        return LLMResponse(
            output=output,
            prompt_token_count=openai_response.usage.prompt_tokens,  # type: ignore
            response_token_count=openai_response.usage.completion_tokens,  # noqa: E501 # type: ignore
        )


class AsyncOpenAIClientV1(BaseOpenAIClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = openai.AsyncClient(
            api_key=self.api_key,
            base_url=self.api_base,
        )

    async def create_embedding(
        self,
        model: str,
        input: List[str],
    ) -> List[List[float]]:
        embeddings = await self.client.embeddings.create(
            model=model,
            input=input,
        )
        return [r.embedding for r in embeddings.data]

    async def create_completion(
        self, engine: str, prompt: str, *args, **kwargs
    ) -> LLMResponse:
        response = await self.client.completions.create(
            model=engine, prompt=prompt, *args, **kwargs
        )

        return await self.construct_nonchat_response(
            stream=kwargs.get("stream", False),
            openai_response=response,
            prompt=prompt,
            engine=engine,
        )

    async def construct_nonchat_response(
        self,
        stream: bool,
        openai_response: Any,
        prompt: str,
        engine: str,
    ) -> LLMResponse:
        if stream:
            # If stream is defined and set to True,
            # openai returns a generator object
            complete_output = ""
            openai_response = cast(AsyncIterable[Dict[str, Any]], openai_response)
            async for response in openai_response:
                complete_output += response["choices"][0]["text"]

            # Also, it no longer returns usage information
            # So manually count the tokens using tiktoken
            prompt_token_count = num_tokens_from_string(
                text=prompt,
                model_name=engine,
            )
            response_token_count = num_tokens_from_string(
                text=complete_output, model_name=engine
            )

            # Return the LLMResponse
            return LLMResponse(
                output=complete_output,
                prompt_token_count=prompt_token_count,
                response_token_count=response_token_count,
            )

        # If stream is not defined or is set to False,
        # extract string from response
        openai_response = cast(Dict[str, Any], openai_response)
        if not openai_response.choices:
            raise ValueError("No choices returned from OpenAI")
        if openai_response.usage is None:
            raise ValueError("No token counts returned from OpenAI")
        return LLMResponse(
            output=openai_response.choices[0].text,  # type: ignore
            prompt_token_count=openai_response.usage.prompt_tokens,  # type: ignore
            response_token_count=openai_response.usage.completion_tokens,  # noqa: E501 # type: ignore
        )

    async def create_chat_completion(
        self, model: str, messages: List[Any], *args, **kwargs
    ) -> LLMResponse:
        response = await self.client.chat.completions.create(
            model=model, messages=messages, *args, **kwargs
        )

        return await self.construct_chat_response(
            stream=kwargs.get("stream", False),
            openai_response=response,
            prompt=messages,
            model=model,
        )

    async def construct_chat_response(
        self,
        stream: bool,
        openai_response: Any,
        prompt: List[Any],
        model: str,
    ) -> LLMResponse:
        """Construct an LLMResponse from an OpenAI response.

        Splits execution based on whether the `stream` parameter is set
        in the kwargs.
        """
        if stream:
            # If stream is defined and set to True,
            # openai returns a generator object
            collected_messages = []
            openai_response = cast(AsyncIterable[Dict[str, Any]], openai_response)
            async for chunk in openai_response:
                chunk_message = chunk["choices"][0]["delta"]
                collected_messages.append(chunk_message)  # save the message

            complete_output = "".join(
                [msg.get("content", "") for msg in collected_messages]
            )

            # Also, it no longer returns usage information
            # So manually count the tokens using tiktoken
            prompt_token_count = num_tokens_from_messages(
                messages=prompt,
                model=model,
            )
            response_token_count = num_tokens_from_string(
                text=complete_output, model_name=model
            )

            # Return the LLMResponse
            return LLMResponse(
                output=complete_output,
                prompt_token_count=prompt_token_count,
                response_token_count=response_token_count,
            )

        # If stream is not defined or is set to False,
        # Extract string from response
        openai_response = cast(Dict[str, Any], openai_response)
        if "function_call" in openai_response["choices"][0]["message"]:  # type: ignore
            output = openai_response["choices"][0]["message"][  # type: ignore
                "function_call"
            ]["arguments"]
        else:
            output = openai_response["choices"][0]["message"]["content"]  # type: ignore

        if not openai_response.choices:
            raise ValueError("No choices returned from OpenAI")
        if not openai_response.choices[0].message.content:
            raise ValueError("No message returned from OpenAI")
        if openai_response.usage is None:
            raise ValueError("No token counts returned from OpenAI")
        return LLMResponse(
            output=output,
            prompt_token_count=openai_response.usage.prompt_tokens,  # type: ignore
            response_token_count=openai_response.usage.completion_tokens,  # noqa: E501 # type: ignore
        )
