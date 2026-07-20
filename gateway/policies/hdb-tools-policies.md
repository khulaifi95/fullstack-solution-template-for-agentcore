# HDB tool Cedar policies (retrieve / run_sql)

The two HDB Gateway tools are gated by the shared AgentCore Policy Engine
(`FAST_stack_policy_engine`), which is deny-by-default. Because that engine is
owned by FAST-stack, the HDB tool permits are added to it directly via the
control-plane API (not through FAST-stack's `policy.cedar`), so FAST-stack is
never redeployed.

Access decision (per user): **any authenticated user** may call the tools.
Document-level access control still happens inside the `retrieve` tool via its
`spaces` metadata filter. The Policy Engine rejects a fully unconditional permit
as "Overly Permissive", so each permit carries the minimal always-present
condition `principal.hasTag("sub")` (every authenticated OAuth principal has a
`sub` claim).

Policies (create once per gateway; idempotent by intent):

```cedar
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"hdb-retrieve___retrieve",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when { principal.hasTag("sub") };

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"hdb-run-sql___run_sql",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when { principal.hasTag("sub") };
```

Apply with:

```
aws bedrock-agentcore-control create-policy \
  --policy-engine-id <ENGINE_ID> --name hdb_retrieve \
  --definition '{"cedar":{"statement":"<statement>"}}'
```

To restrict access later (e.g. by department/group), replace the `when`
condition with the appropriate `principal.getTag(...)` checks — same pattern as
FAST's sample-tool policy.
