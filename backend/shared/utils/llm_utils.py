from openai import AsyncAzureOpenAI

class AzureOpenAILLM:
    """Small Azure OpenAI client wrapper."""

    def __init__(self, client: AsyncAzureOpenAI, deployment: str):
        from shared.ai.provider import assert_legacy_allowed
        assert_legacy_allowed()
        self.deployment = deployment
        self.client = client

    async def ainvoke(self, prompt: str) -> str:
        from shared.ai.provider import assert_legacy_allowed
        assert_legacy_allowed()
        response = await self.client.chat.completions.create(
            model=self.deployment,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("Azure OpenAI returned an empty response.")
        return content

    async def extract_structured(self, prompt, schema, *, system=None, **kwargs):
        from shared.ai.provider import assert_legacy_allowed
        assert_legacy_allowed()
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        response = await self.client.beta.chat.completions.parse(
            model=self.deployment, messages=messages, response_format=schema,
            temperature=0,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError("Azure returned no structured output")
        return schema.model_validate(parsed.model_dump())
