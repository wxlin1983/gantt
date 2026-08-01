/**
 * The directory: users and groups (design.md §11, implement.md §7.1).
 *
 * Until now people existed only in `gantt seed` and the database, so adding a
 * colleague meant opening a shell -- and the case wizard, having no list to
 * offer, took a typed username and accepted one that matched nobody.
 *
 * Read-only for everyone signed in, editable by administrators. Seeing who
 * your colleagues are is not privileged information in a coordination tool,
 * and the wizard needs the list to do its job.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, api } from "../../api/client";
import type { GroupDetail, Person } from "../../api/types";

export function People() {
  const client = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const users = useQuery({ queryKey: ["users"], queryFn: api.users });
  const groups = useQuery({ queryKey: ["groups"], queryFn: api.groups });

  const canManage = me.data?.user.is_template_admin ?? false;
  const refresh = () => {
    client.invalidateQueries({ queryKey: ["users"] });
    client.invalidateQueries({ queryKey: ["groups"] });
  };
  const onError = (err: unknown) =>
    setError(err instanceof ApiError ? err.message : String(err));

  return (
    <section className="page">
      <header className="page-head">
        <h1>People</h1>
      </header>
      {!canManage && (
        <p className="muted">
          Read-only. An administrator can add and edit people here.
        </p>
      )}
      {error && (
        <p className="tone-danger" role="alert">
          {error}
        </p>
      )}

      <h3>Users</h3>
      <UserTable
        users={users.data ?? []}
        groups={groups.data ?? []}
        canManage={canManage}
        meId={me.data?.user.id}
        onDone={() => {
          setError(null);
          refresh();
        }}
        onError={onError}
      />
      {canManage && <NewUser onDone={refresh} onError={onError} />}

      <h3>Groups</h3>
      <GroupTable
        groups={groups.data ?? []}
        users={users.data ?? []}
        canManage={canManage}
        onDone={() => {
          setError(null);
          refresh();
        }}
        onError={onError}
      />
      {canManage && <NewGroup onDone={refresh} onError={onError} />}
    </section>
  );
}

function UserTable({
  users,
  groups,
  canManage,
  meId,
  onDone,
  onError,
}: {
  users: Person[];
  groups: GroupDetail[];
  canManage: boolean;
  meId?: number;
  onDone: () => void;
  onError: (err: unknown) => void;
}) {
  const client = useQueryClient();
  const update = useMutation({
    mutationFn: (payload: { id: number; body: Record<string, unknown> }) =>
      api.updateUser(payload.id, payload.body),
    onSuccess: onDone,
    onError,
  });
  const setPassword = useMutation({
    mutationFn: (payload: { id: number; password: string }) =>
      api.setUserPassword(payload.id, payload.password),
    onSuccess: () => client.invalidateQueries({ queryKey: ["users"] }),
    onError,
  });

  const groupName = (id: number) =>
    groups.find((group) => group.id === id)?.name ?? `#${id}`;

  return (
    <table className="table">
      <thead>
        <tr>
          <th>User</th>
          <th>Email</th>
          <th>Groups</th>
          <th>Admin</th>
          <th>Status</th>
          {canManage && <th />}
        </tr>
      </thead>
      <tbody>
        {users.map((person) => (
          <tr key={person.id} className={person.is_active ? "" : "muted"}>
            <td>
              <span className="strong">{person.display_name}</span>
              <div className="muted small">{person.username}</div>
            </td>
            <td className="muted">{person.email || "—"}</td>
            <td className="muted small">
              {person.memberships.length === 0
                ? "—"
                : person.memberships
                    .map(
                      (m) =>
                        groupName(m.group_id) + (m.is_lead ? " (lead)" : ""),
                    )
                    .join(", ")}
            </td>
            <td>
              <input
                type="checkbox"
                checked={person.is_template_admin}
                // Your own admin bit is the one you cannot clear; the server
                // refuses it too, this only avoids offering the mistake.
                disabled={!canManage || person.id === meId}
                onChange={(event) =>
                  update.mutate({
                    id: person.id,
                    body: { is_template_admin: event.target.checked },
                  })
                }
              />
            </td>
            <td>
              {person.is_active ? (
                <span className="tone-success small">active</span>
              ) : (
                <span className="muted small">deactivated</span>
              )}
              {!person.has_password && (
                <span className="pill" title="Cannot sign in yet">
                  no password
                </span>
              )}
            </td>
            {canManage && (
              <td>
                <div className="row-actions">
                  <button
                    type="button"
                    className="button small"
                    onClick={() => {
                      const next = window.prompt(
                        `New password for ${person.username} ` +
                          "(at least 8 characters)",
                      );
                      if (next)
                        setPassword.mutate({
                          id: person.id,
                          password: next,
                        });
                    }}
                  >
                    Set password
                  </button>
                  <button
                    type="button"
                    className="button small"
                    disabled={person.id === meId}
                    onClick={() =>
                      update.mutate({
                        id: person.id,
                        body: { is_active: !person.is_active },
                      })
                    }
                  >
                    {person.is_active ? "Deactivate" : "Reactivate"}
                  </button>
                </div>
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function NewUser({
  onDone,
  onError,
}: {
  onDone: () => void;
  onError: (err: unknown) => void;
}) {
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.createUser({
        username,
        display_name: displayName,
        email,
        // Omitted rather than sent empty: a user with no local hash cannot
        // sign in, which is a legitimate state for an account waiting on one.
        ...(password ? { password } : {}),
      }),
    onSuccess: () => {
      setUsername("");
      setDisplayName("");
      setEmail("");
      setPassword("");
      onDone();
    },
    onError,
  });

  return (
    <div className="inline-form">
      <input
        value={username}
        placeholder="username *"
        onChange={(event) => setUsername(event.target.value)}
      />
      <input
        value={displayName}
        placeholder="Display name"
        onChange={(event) => setDisplayName(event.target.value)}
      />
      <input
        value={email}
        placeholder="email"
        onChange={(event) => setEmail(event.target.value)}
      />
      <input
        type="password"
        value={password}
        placeholder="password (optional)"
        onChange={(event) => setPassword(event.target.value)}
      />
      <button
        type="button"
        className="button primary"
        disabled={!username.trim() || create.isPending}
        onClick={() => create.mutate()}
      >
        Add user
      </button>
    </div>
  );
}

function GroupTable({
  groups,
  users,
  canManage,
  onDone,
  onError,
}: {
  groups: GroupDetail[];
  users: Person[];
  canManage: boolean;
  onDone: () => void;
  onError: (err: unknown) => void;
}) {
  const [editing, setEditing] = useState<number | null>(null);

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteGroup(id),
    onSuccess: onDone,
    onError,
  });

  return (
    <>
      <table className="table">
        <thead>
          <tr>
            <th>Group</th>
            <th>Members</th>
            <th>Lead</th>
            {canManage && <th />}
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => (
            <tr key={group.id}>
              <td>
                <span className="strong">{group.display_name}</span>
                <div className="muted small">{group.name}</div>
              </td>
              <td className="muted small">
                {group.members.length === 0
                  ? "—"
                  : group.members.map((m) => m.display_name).join(", ")}
              </td>
              <td className="muted small">
                {group.members
                  .filter((m) => m.is_lead)
                  .map((m) => m.display_name)
                  .join(", ") || "—"}
              </td>
              {canManage && (
                <td>
                  <div className="row-actions">
                    <button
                      type="button"
                      className="button small"
                      onClick={() => setEditing(group.id)}
                    >
                      Members
                    </button>
                    <button
                      type="button"
                      className="button small"
                      onClick={() => {
                        if (
                          window.confirm(`Delete the group ${group.name}?`)
                        )
                          remove.mutate(group.id);
                      }}
                    >
                      Delete
                    </button>
                  </div>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>

      {editing !== null && (
        <MembersDialog
          group={groups.find((group) => group.id === editing)!}
          users={users}
          onClose={() => setEditing(null)}
          onDone={() => {
            setEditing(null);
            onDone();
          }}
          onError={onError}
        />
      )}
    </>
  );
}

/**
 * The whole membership list, saved in one go.
 *
 * The server replaces it wholesale rather than merging, which matches what
 * this dialog shows: a set of checkboxes with one Save.
 */
function MembersDialog({
  group,
  users,
  onClose,
  onDone,
  onError,
}: {
  group: GroupDetail;
  users: Person[];
  onClose: () => void;
  onDone: () => void;
  onError: (err: unknown) => void;
}) {
  const [members, setMembers] = useState<Map<number, boolean>>(
    () => new Map(group.members.map((m) => [m.user_id, m.is_lead])),
  );

  const save = useMutation({
    mutationFn: () =>
      api.setGroupMembers(
        group.id,
        [...members].map(([user_id, is_lead]) => ({ user_id, is_lead })),
      ),
    onSuccess: onDone,
    onError,
  });

  const toggle = (id: number, on: boolean) => {
    const next = new Map(members);
    if (on) next.set(id, next.get(id) ?? false);
    else next.delete(id);
    setMembers(next);
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <header>
          <h2>{group.display_name} members</h2>
          <button type="button" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>
        <table className="table compact">
          <thead>
            <tr>
              <th>Member</th>
              <th>Lead</th>
            </tr>
          </thead>
          <tbody>
            {users
              .filter((person) => person.is_active)
              .map((person) => (
                <tr key={person.id}>
                  <td>
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        checked={members.has(person.id)}
                        onChange={(event) =>
                          toggle(person.id, event.target.checked)
                        }
                      />
                      {person.display_name}
                    </label>
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      checked={members.get(person.id) ?? false}
                      disabled={!members.has(person.id)}
                      onChange={(event) => {
                        const next = new Map(members);
                        next.set(person.id, event.target.checked);
                        setMembers(next);
                      }}
                    />
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
        <div className="toolbar">
          <button
            type="button"
            className="button primary"
            disabled={save.isPending}
            onClick={() => save.mutate()}
          >
            Save members
          </button>
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

function NewGroup({
  onDone,
  onError,
}: {
  onDone: () => void;
  onError: (err: unknown) => void;
}) {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.createGroup({ name, display_name: displayName }),
    onSuccess: () => {
      setName("");
      setDisplayName("");
      onDone();
    },
    onError,
  });

  return (
    <div className="inline-form">
      <input
        value={name}
        placeholder="name * (as templates refer to it)"
        onChange={(event) => setName(event.target.value)}
      />
      <input
        value={displayName}
        placeholder="Display name"
        onChange={(event) => setDisplayName(event.target.value)}
      />
      <button
        type="button"
        className="button primary"
        disabled={!name.trim() || create.isPending}
        onClick={() => create.mutate()}
      >
        Add group
      </button>
    </div>
  );
}
