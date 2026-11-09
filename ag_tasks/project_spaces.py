"""Explicit project-to-space ownership shared by every task-creation adapter."""

import re

from .issues import DomainError, bounded_text, timestamp
from .service_values import fields

def project_key(value):
    """
    Normalize an explicit stable project key without interpreting local filesystem paths.

    Args:
        value: ASCII project identity, for example ag-tasks or github.com/org/repo.
    Returns:
        Lowercase canonical key of at most 128 characters.
    Raises:
        DomainError: The key is empty, malformed or outside the bounded contract.
    """
    value = bounded_text(value, 128, empty=False).strip().lower()
    if not re.fullmatch(r'[a-z][a-z0-9._/-]{0,127}', value) or '//' in value:
        raise DomainError('invalid_project', 'Use a stable ASCII project key, not an absolute checkout path.')
    return value


def migrate(connection):
    """
    Add the project registry without changing existing task ownership or definitions.

    Args:
        connection: Owning SQLite connection during startup schema migration.
    """
    connection.execute('''CREATE TABLE IF NOT EXISTS project_spaces(
        project_key TEXT PRIMARY KEY COLLATE NOCASE,
        space_id TEXT NOT NULL UNIQUE REFERENCES spaces(id),created_at TEXT NOT NULL)''')


class ProjectSpaces:
    """Bind one stable project to one organizational space; bindings grant no repository access."""

    def __init__(self, store):
        self.store = store

    def get(self, key, connection):
        """
        Read an active project binding, rejecting archived or unknown owners.

        Args:
            key: Explicit project key, normalized by this method.
            connection: Owning read/write connection.
        Returns:
            A joined project/space SQLite row.
        Raises:
            DomainError: The project is unknown or its space is archived.
        """
        key = project_key(key)
        row = connection.execute('''SELECT p.project_key,s.* FROM project_spaces p
            JOIN spaces s ON s.id=p.space_id WHERE p.project_key=?''', (key,)).fetchone()
        if row is None or row['archived_at'] is not None:
            raise DomainError('project_not_found', 'Ensure an active project space before creating its tasks.', 404)
        return row

    def ensure(self, payload):
        """
        Find or create a project space atomically; repeating a key never makes a duplicate.

        Args:
            payload: projectKey and name, with optional explicit prefix for a new space.
        Returns:
            Space metadata, projectKey and whether this call created a space.
        Raises:
            DomainError: Identity/prefix is invalid or a named space has another owner.

        The command journal supplies durable request identity. A different request
        for the same project still reuses its existing name and prefix unchanged.
        """
        fields(payload, {'projectKey', 'name', 'prefix'}, {'projectKey', 'name'})
        key = project_key(payload['projectKey'])
        name = bounded_text(payload['name'], 128, empty=False).strip()
        with self.store.db.transaction() as connection:
            bound = connection.execute('SELECT space_id FROM project_spaces WHERE project_key=?', (key,)).fetchone()
            created = False
            if bound:
                space_id = self.get(key, connection)['id']
            else:
                matches = connection.execute('SELECT id FROM spaces WHERE lower(trim(name))=lower(?) AND archived_at IS NULL',
                                             (name,)).fetchall()
                if len(matches) > 1:
                    raise DomainError('ambiguous_space', 'Several spaces have that name; select a unique project name.', 409)
                if matches:
                    space_id = matches[0]['id']
                    if space_id == 'default' or connection.execute('SELECT 1 FROM project_spaces WHERE space_id=?', (space_id,)).fetchone():
                        raise DomainError('project_space_conflict', 'This space already has a different organizational owner.', 409)
                else:
                    definition = {'name': name}
                    if 'prefix' in payload:
                        definition['prefix'] = payload['prefix']
                    space_id = self.store.spaces.write(definition)['id']
                    created = True
                connection.execute('INSERT INTO project_spaces VALUES(?,?,?)', (key, space_id, timestamp()))
            space = next(item for item in self.store.spaces.list() if item['id'] == space_id)
            return {**space, 'projectKey': key, 'created': created}

    def resolve_creation(self, data, actor, connection):
        """
        Resolve a new task's project before normal task validation and key allocation.

        Args:
            data: Submitted creation fields; transient project fields are removed from the result.
            actor: Declared user/agent provenance, used for project admission.
            connection: Current task creation transaction.
        Returns:
            Ordinary task fields with the exact selected spaceId.
        Raises:
            DomainError: A project conflicts with its parent/space, or agent code work lacks a project.
        """
        result = dict(data)
        key = result.pop('projectKey', None)
        name = result.pop('projectName', None)
        prefix = result.pop('projectPrefix', None)
        if key is not None:
            definition = {'projectKey': key, 'name': name or project_key(key)}
            if prefix is not None:
                definition['prefix'] = prefix
            chosen = self.ensure(definition)['id']
            if result.get('spaceId') not in (None, chosen):
                raise DomainError('project_space_conflict', 'The explicit space does not belong to this project.', 409)
            result['spaceId'] = chosen
        elif name is not None or prefix is not None:
            raise DomainError('project_required', 'Project name/prefix require an explicit projectKey.')
        if result.get('parentId'):
            parent_space = self.store._row(result['parentId'], connection)['space_id']
            if result.get('spaceId') not in (None, parent_space):
                raise DomainError('cross_space_relation', 'Children inherit their parent project space.', 409)
            result['spaceId'] = parent_space
        elif actor == 'agent' and (result.get('codeTask') or result.get('repoId') or result.get('languages') or result.get('systems')):
            if result.get('spaceId') in (None, 'default'):
                raise DomainError('project_required', 'Project work requires projectKey or an existing project space; ensure it first.', 409)
        return result
