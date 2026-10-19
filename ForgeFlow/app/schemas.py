"""Validated contracts between the model, agents, and tools."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Plan(BaseModel):
    summary: str
    tasks: list[str] = Field(min_length=1, max_length=12)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=15)
    affected_files: list[str] = Field(default_factory=list, max_length=30)
    regression_scenarios: list[str] = Field(default_factory=list, max_length=15)
    risk: Literal["low", "medium", "high"]
    risk_reason: str
    supported: bool = True
    unsupported_reason: str = ""


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal[
        "read_file",
        "list_files",
        "search_code",
        "write_file",
        "edit_file",
        "delete_file",
        "create_directory",
        "run_command",
    ]
    arguments: dict = Field(default_factory=dict)


class DeveloperResponse(BaseModel):
    summary: str
    actions: list[Action] = Field(default_factory=list, max_length=12)
    finished: bool = False


class AcceptanceSuite(BaseModel):
    explanation: str
    test_code: str = Field(min_length=20, max_length=60000)
    covered_criteria: list[int] = Field(default_factory=list)
    unverified_criteria: list[int] = Field(default_factory=list)


class ValidationDecision(BaseModel):
    accepted: bool
    summary: str
    missing_requirements: list[str] = Field(default_factory=list)
    requires_code_changes: bool = True


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class ModelSelection(BaseModel):
    profile: Literal["default", "gpt"]


class BrowserStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "fill",
        "click",
        "press",
        "focus",
        "visible",
        "hidden",
        "text",
        "attribute",
        "focused",
        "same_url",
        "viewport",
        "no_overflow",
        "in_viewport",
        "css",
        "scrolled",
    ]
    selector: str = Field(default="", max_length=400)
    value: str = Field(default="", max_length=1000)
    attribute: str = Field(default="", max_length=80)
    width: int = Field(default=390, ge=320, le=1920)
    height: int = Field(default=844, ge=480, le=1440)
    minimum_scroll: int = Field(default=1, ge=0, le=100000)

    @model_validator(mode="after")
    def validate_target(self):
        if self.action not in {"viewport", "same_url", "no_overflow", "scrolled"} and not self.selector:
            raise ValueError("A browser interaction requires a CSS selector.")
        if self.action in {"attribute", "css"} and not self.attribute:
            raise ValueError("An attribute assertion requires an attribute name.")
        return self


class BrowserScenario(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    javascript_enabled: bool = True
    reduced_motion: Literal["reduce", "no-preference"] = "no-preference"
    steps: list[BrowserStep] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def require_assertion(self):
        assertions = {"visible", "hidden", "text", "attribute", "focused", "no_overflow", "in_viewport", "css", "scrolled"}
        if not any(step.action in assertions for step in self.steps):
            raise ValueError("A browser scenario must assert an observable result.")
        return self


class BrowserSuite(BaseModel):
    scenarios: list[BrowserScenario] = Field(default_factory=list, max_length=12)
    limitations: list[str] = Field(default_factory=list, max_length=12)
