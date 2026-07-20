"""Strands agent with Gateway MCP tools, Memory, and Code Interpreter."""

import json
import logging
import os

from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from strands import Agent
from strands.models import BedrockModel
from tools.gateway import create_gateway_mcp_client
from utils.auth import extract_user_id_from_context

from tools.code_interpreter import StrandsCodeInterpreterTools

logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

# HDB/HCSA knowledge-management RAG orchestrator (see
# docs/HDB_KM_CHATBOT_ARCHITECTURE.md). The agent answers officer questions
# strictly from the HDB knowledge base, routing between two Gateway tools:
#   - retrieve : semantic search over policies, SOPs, emails, and reports (docs)
#   - run_sql  : read-only SQL over the structured datasets (contractors,
#                development projects, permits, inspections)
# and fusing both when a question needs documents AND structured facts.
SYSTEM_PROMPT = (
    "You are the HDB knowledge-management assistant for officers of HDB "
    "(also referred to as HCSA — treat HDB and HCSA as the same organization). "
    "You answer questions ONLY from HDB's own records, retrieved through your tools. "
    "You never rely on outside or prior knowledge, and you never guess.\n\n"
    "TOOLS AND ROUTING:\n"
    "- Use `retrieve` for anything answerable from documents: policies and SOPs, "
    "email correspondence, and financial/annual reports. It returns text chunks "
    "each with a source document, space, and page number.\n"
    "- Use `run_sql` for questions about the structured datasets — contractors, "
    "development projects, permits, and inspections — especially counts, sums, "
    "rankings, or relationships across those tables. Pass the statement in the "
    "`sql` argument (NOT `query`) as a single read-only SELECT in Athena/Presto "
    "syntax; the tool description lists the schema, join keys, and data-quality "
    "caveats (match text status columns case-insensitively). If a tool call "
    "returns an error, read the error message and retry with corrected arguments "
    "— do not conclude the tool is unavailable.\n"
    "- Some questions need BOTH: retrieve the relevant policy/passage AND query "
    "the structured data, then combine them into one answer.\n"
    "- If a question is ambiguous about which records it concerns, retrieve first "
    "to ground yourself, then decide.\n\n"
    "GROUNDING AND CITATIONS (highest priority):\n"
    "- Base every factual statement on tool results from THIS conversation. If the "
    "tools return nothing relevant, say you could not find the information in the "
    "HDB records — do not fabricate an answer.\n"
    "- Cite your sources. For document answers, name the source file and page "
    "(e.g. 'SOP-CO-003.pdf' or 'HDB FS-22.pdf, page 36'). For structured answers, "
    "state that the figures come from the structured datasets and name the tables.\n"
    "- Prefer accuracy over completeness, and completeness over length. Answer the "
    "question directly and include the key points; do not pad with irrelevant "
    "context or restate the question.\n"
)


def _create_session_manager(
    user_id: str, session_id: str
) -> AgentCoreMemorySessionManager:
    """Create an AgentCore memory session manager, optionally with long-term semantic retrieval.

    When the USE_LONG_TERM_MEMORY environment variable is "true", configures retrieval
    from the /facts/{actorId} namespace so the agent recalls facts across sessions.
    When false (default), only short-term memory (conversation history) is active,
    avoiding the additional storage and retrieval costs of long-term memory.

    Args:
        user_id: Unique identifier for the user (actor), extracted from the JWT sub claim.
        session_id: Unique identifier for the current conversation session.

    Returns:
        An AgentCoreMemorySessionManager bound to the user and session.
    """
    memory_id = os.environ.get("MEMORY_ID")
    if not memory_id:
        raise ValueError("MEMORY_ID environment variable is required")

    use_ltm = os.environ.get("USE_LONG_TERM_MEMORY", "false").lower() == "true"

    top_k = int(os.environ.get("LTM_TOP_K", "10"))
    relevance_score = float(os.environ.get("LTM_RELEVANCE_SCORE", "0.3"))

    # Only pass retrieval_config when LTM is explicitly enabled.
    # Omitting it means the session manager uses short-term memory only,
    # which avoids the $0.50/1,000 retrieval and $0.75/1,000 storage costs.
    retrieval_config = (
        {
            "/facts/{actorId}": RetrievalConfig(
                top_k=top_k,
                relevance_score=relevance_score,
            )
        }
        if use_ltm
        else None
    )

    config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=session_id,
        actor_id=user_id,
        retrieval_config=retrieval_config,
    )
    return AgentCoreMemorySessionManager(
        agentcore_memory_config=config,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )


def create_strands_agent(user_id: str, session_id: str) -> Agent:
    """Create a Strands agent with Gateway tools, memory, and Code Interpreter."""

    bedrock_model = BedrockModel(
        model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0", temperature=0.1
    )

    session_manager = _create_session_manager(user_id, session_id)

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    code_tools = StrandsCodeInterpreterTools(region)

    gateway_client = create_gateway_mcp_client(user_id)

    return Agent(
        name="strands_agent",
        system_prompt=SYSTEM_PROMPT,
        tools=[gateway_client, code_tools.execute_python_securely],
        model=bedrock_model,
        session_manager=session_manager,
        trace_attributes={"user.id": user_id, "session.id": session_id},
    )


@app.entrypoint
async def invocations(payload, context: RequestContext):
    """Main entrypoint — called by AgentCore Runtime on each request.

    Extracts user ID from the validated JWT token (not the payload body)
    to prevent impersonation via prompt injection.
    """
    user_query = payload.get("prompt")
    session_id = payload.get("runtimeSessionId")

    if not all([user_query, session_id]):
        yield {
            "status": "error",
            "error": "Missing required fields: prompt or runtimeSessionId",
        }
        return

    try:
        user_id = extract_user_id_from_context(context)
        agent = create_strands_agent(user_id, session_id)

        async for event in agent.stream_async(user_query):
            yield json.loads(json.dumps(dict(event), default=str))

    except Exception as e:
        logger.exception("Agent run failed")
        yield {"status": "error", "error": str(e)}


if __name__ == "__main__":
    app.run()
