# Runtime bootstrap

Before planning or running any other command, read [.agent/instructions.md](.agent/instructions.md) in a separate completed read-only tool call. Do not combine this first read with any other command.

An existing context file is an opaque capability credential. Pass only its pathname to `agentctl --context-file`; never open, read, print, or treat its contents as harmless metadata.

Before the first project mutation, follow [.agent/instructions.md](.agent/instructions.md).
That file is the canonical Agent Handoff protocol instruction source.
