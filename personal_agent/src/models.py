from typing import Any, Optional
from pydantic import BaseModel, Field


class AgentOutput(BaseModel):
    """Structured output produced by the browser agent."""
    success: bool = Field(description="True if the task completed successfully, False otherwise")
    error: Optional[str] = Field(
        default=None,
        description="Reason for failure if success is False, otherwise null",
    )
    result: Optional[Any] = Field(
        default=None,
        description="Task result data in any structured form",
    )
    source: str = Field(
        default="unknown",
        description="Website used to complete the task",
    )


class TaskInput(BaseModel):
    task: str = Field(description="Task for the agent to perform, including the target URL")


class TaskResponse(BaseModel):
    """Full API response returned to the caller."""
    success: bool
    error: Optional[str] = None
    result: Optional[Any] = None
    source: str
    completed_at: str
