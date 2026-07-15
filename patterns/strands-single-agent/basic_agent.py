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
from tools.knowledge_base import search_knowledge_base
from tools.structured_data import describe_structured_data, query_structured_data

logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = (
    "You are a helpful assistant for the Housing, Construction & Sustainability "
    "Authority (HCSA/HDB). You have access to tools via the Gateway and Code "
    "Interpreter, and—when configured—a document knowledge base and structured "
    "datasets.\n\n"
    "When a knowledge base tool is available, use `search_knowledge_base` for "
    "questions answerable from policies, SOPs, emails, or reports, and ALWAYS "
    "cite the source document(s) for facts you draw from it. Use "
    "`query_structured_data` for questions about contractors, development "
    "projects, permits, or inspections (counts, statuses, ratings, costs, joins). "
    "When a structured-data question uses fuzzy or natural wording (e.g. "
    "'troubled contractors', 'overdue permits', 'top builders'), FIRST call "
    "`describe_structured_data` to see the real column names and their exact "
    "allowed values, then map the question to precise filters — a filter value "
    "can be a list to match several values at once (e.g. "
    "{\"Financial Health Rating\": [\"UNDER_REVIEW\", \"FAIR\"]}). "
    "Some questions need both — retrieve from documents AND query the structured "
    "data, then combine the answer. If a tool returns no relevant results, say so "
    "rather than guessing.\n\n"
    "When asked about your tools, list them and explain what they do."
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

    tools = [gateway_client, code_tools.execute_python_securely]

    # RAG tools are opt-in: only register them when the KB/table were deployed
    # (env vars set by the CDK backend construct when use_knowledge_base is true).
    if os.environ.get("KNOWLEDGE_BASE_ID"):
        tools.append(search_knowledge_base)
    if os.environ.get("STRUCTURED_TABLE_NAME"):
        tools.append(describe_structured_data)
        tools.append(query_structured_data)

    return Agent(
        name="strands_agent",
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
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
