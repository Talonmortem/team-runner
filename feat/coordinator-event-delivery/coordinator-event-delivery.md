# Coordinator event delivery

Current Orca reports activity=unknown for an exact live idle terminal. The existing idle-only guard prevents every automatic child-result wake. Replace terminal input with orchestration send addressed to the owned coordinator Dispatch; retain ownership/incarnation validation, durable request IDs, processing ACK and conservative legacy reconciliation. A durable message receipt proves queueing, not processing.

Verify unknown/working activity, completed/error receipt, absent/pending/lost responses, duplicate batches/restart, cancellation, identity mismatch and separate project journals. Preserve old results and snapshots. Deploy surgical modules only; reload SHM service only, stop settled SHM resources, retry with saved settings and explicit resume. No unrelated project control.
