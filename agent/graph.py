from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agent.tools import ToolRegistry

RUN_CONTEXT_KEY = "run_context"
RECURSION_LIMIT = 16


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def graph_config(ctx) -> dict:
    return {"recursion_limit": RECURSION_LIMIT, "configurable": {RUN_CONTEXT_KEY: ctx}}


def build_graph(model, registry: ToolRegistry):
    async def model_node(state: AgentState, config: RunnableConfig):
        reply = await model.ainvoke(state["messages"])
        return {"messages": [reply]}

    async def tools_node(state: AgentState, config: RunnableConfig):
        ctx = config["configurable"][RUN_CONTEXT_KEY]
        results = []
        for call in state["messages"][-1].tool_calls:
            outcome = await registry.execute(ctx, call["name"], call["args"])
            results.append(
                ToolMessage(
                    content=outcome.text,
                    tool_call_id=call["id"],
                    name=call["name"],
                    status="success" if outcome.ok else "error",
                )
            )
        return {"messages": results}

    def route_after_model(state: AgentState) -> str:
        return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END

    def route_after_tools(state: AgentState, config: RunnableConfig) -> str:
        ctx = config["configurable"][RUN_CONTEXT_KEY]
        return END if ctx.ledger.limit_hit else "model"

    graph = StateGraph(AgentState)
    graph.add_node("model", model_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", route_after_model, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", route_after_tools, {"model": "model", END: END})
    return graph.compile()
