class AiService:
    async def analyze_topic(self, text: str) -> dict:
        raise NotImplementedError

    async def generate_comment(self, text: str) -> str:
        raise NotImplementedError
