from openai import AsyncAzureOpenAI

class AzureOpenAILLM:
    """Small Azure OpenAI client wrapper."""

    def __init__(self, client: AsyncAzureOpenAI, deployment: str):
        self.deployment = deployment
        self.client = client

    async def ainvoke(self, prompt: str) -> str:
        response = await self.client.chat.completions.create(
            model=self.deployment,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("Azure OpenAI returned an empty response.")
        return content