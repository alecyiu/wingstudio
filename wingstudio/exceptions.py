"""Exceptions shared by the engine and the agent runtimes."""


class EngineError(Exception):
    """A request the engine refuses; `status` is the HTTP code to answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class AgentError(Exception):
    """An agent could not be run (missing definition, bad frontmatter, LLM failure)."""


class Interrupt(Exception):
    """Unwinds an agent session at a tool boundary. kind: stop | rerun."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind
