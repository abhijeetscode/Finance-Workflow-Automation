def build_agent():
	from .agent import build_agent as _build_agent

	return _build_agent()


__all__ = ["build_agent"]
