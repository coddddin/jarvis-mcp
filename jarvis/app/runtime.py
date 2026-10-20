"""Shared configuration, services, and mandatory tool admission boundary."""
from jarvis.core.paths import DEFAULT_WORKSPACE
import logging
import os
from pathlib import Path
from dataclasses import dataclass
from functools import wraps
from mcp.server.fastmcp.exceptions import ToolError
from jarvis.core.guardrails import GuardrailError, Guardrails
from jarvis.features.knowledge.service import KnowledgeStore, KnowledgeError
from jarvis.features.google.service import GoogleBriefing, GoogleIntegrationError
from jarvis.features.snapshots.service import SnapshotStore, SnapshotError
from jarvis.features.files.service import FileOrganizer, OrganizationError
from jarvis.features.workflows.service import WorkflowLauncher, WorkflowError
from jarvis.features.development.service import DebugAssistant, DebugError
from jarvis.features.web.service import WebCollector, WebError
from jarvis.features.integrations.service import IntegrationHub, IntegrationError


@dataclass(frozen=True)
class ToolContext:
    guard: Guardrails
    knowledge: KnowledgeStore
    google: GoogleBriefing
    snapshots: SnapshotStore
    organizer: FileOrganizer
    workflows: WorkflowLauncher
    debug: DebugAssistant
    web: WebCollector
    integrations: IntegrationHub

    @classmethod
    def create(cls, guard):
        snapshots = SnapshotStore(guard)
        web = WebCollector(snapshots)
        return cls(
            guard=guard,
            knowledge=KnowledgeStore(guard),
            google=GoogleBriefing(),
            snapshots=snapshots,
            organizer=FileOrganizer(snapshots),
            workflows=WorkflowLauncher(guard),
            debug=DebugAssistant(snapshots),
            web=web,
            integrations=IntegrationHub(guard, web),
        )


def guarded_tool(function, context):
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            context.guard.enter_call()
            return function(*args, **kwargs)
        except ToolError:
            raise
        except (GuardrailError, KnowledgeError, GoogleIntegrationError, SnapshotError, OrganizationError, WorkflowError, DebugError, WebError, IntegrationError) as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            logging.getLogger('jarvis').error('Tool %s failed (%s)', function.__name__, type(exc).__name__)
            raise ToolError('Operation failed; check local data, permissions and configuration.') from exc
    return invoke

def load_guardrails() -> Guardrails:
    configured = os.environ.get('JARVIS_WORKSPACE')
    if configured is None:
        workspace = DEFAULT_WORKSPACE
        if workspace.is_symlink() or workspace.is_junction():
            raise GuardrailError('Default workspace cannot be a link or junction.')
        workspace.mkdir(exist_ok=True)
    elif not configured.strip():
        raise GuardrailError('JARVIS_WORKSPACE cannot be empty.')
    else:
        workspace = Path(configured)
        if workspace.is_symlink() or workspace.is_junction():
            raise GuardrailError('Configured workspace cannot be a link or junction.')
    return Guardrails(workspace)
