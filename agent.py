import os
from pathlib import Path

from agno.agent import Agent
from agno.models.groq import Groq
from agno.models.openai import OpenAIChat
from agno.playground import Playground, serve_playground_app
from agno.storage.sqlite import SqliteStorage
from agno.tools.tavily import TavilyTools
from dotenv import load_dotenv

from reader import (
    get_creator_patterns,
    get_creator_transcriptions,
    get_transcription_library,
    list_available_creators,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

instructions_path = BASE_DIR / "prompts" / "copywriter.md"
instructions = (
    instructions_path.read_text(encoding="utf-8") if instructions_path.exists() else ""
)


def build_model():
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return OpenAIChat(id="gpt-4.1-mini", temperature=0)

    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        return Groq(id=groq_model, api_key=groq_key, temperature=0)

    raise SystemExit(
        "Missing model credentials. Set OPENAI_API_KEY or GROQ_API_KEY in .env."
    )


def build_tools():
    tools = [
        get_transcription_library,
        get_creator_patterns,
        list_available_creators,
        get_creator_transcriptions,
    ]
    if os.getenv("TAVILY_API_KEY"):
        tools.insert(0, TavilyTools())
    return tools


tmp_dir = BASE_DIR / "tmp"
tmp_dir.mkdir(parents=True, exist_ok=True)


copywriter = Agent(
    model=build_model(),
    name="copywriter",
    add_history_to_messages=True,
    num_history_runs=10,
    storage=SqliteStorage(
        table_name="agent_sessions",
        db_file=str(tmp_dir / "storage.db"),
    ),
    tools=build_tools(),
    show_tool_calls=True,
    instructions=instructions,
)

app = Playground(agents=[copywriter]).get_app(use_async=False, prefix="")

if __name__ == "__main__":
    host = os.getenv("PLAYGROUND_HOST", "127.0.0.1")
    port = int(os.getenv("PLAYGROUND_PORT", "8000"))
    serve_playground_app(app, host=host, port=port, prefix="", reload=False)
