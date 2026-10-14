import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import type { Session, User } from "./api";
import {
  absoluteInviteUrl,
  canAdminister,
  messageOf,
  useCreateInvitation,
  useRevokeInvitation,
  useUpdateUser,
  useUsers,
  type Invitation,
  type Role,
  type UserChanges,
} from "./adminApi";
import { LocalTime } from "./CSLPanel";
import { Field } from "./SettingsControls";
import "./admin.css";

const ROLE_LABELS: Record<Role, string> = { admin: "Admin", reviewer: "Reviewer" };

function InviteForm() {
  const invite = useCreateInvitation();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("reviewer");
  const [copy, setCopy] = useState<"idle" | "copied" | "failed">("idle");
  const reset = useRef(invite.reset);
  reset.current = invite.reset;
  // The link is a one-use secret: drop it when this panel goes away.
  useEffect(() => () => reset.current(), []);
  const created = invite.data;
  const link = created ? absoluteInviteUrl(created.invite_url) : null;

  function submit(event: FormEvent) {
    event.preventDefault();
    setCopy("idle");
    invite.mutate(
      { email: email.trim(), role },
      { onSuccess: () => setEmail("") },
    );
  }
  async function copyLink() {
    if (!link) return;
    try {
      if (!navigator.clipboard) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(link);
      setCopy("copied");
    } catch {
      setCopy("failed");
    }
  }

  return (
    <div className="admin-subsection">
      <h3>Invite someone</h3>
      <p className="admin-muted">
        Creates a one-use link for you to share. Inviting the same email again
        withdraws the earlier link.
      </p>
      <form className="admin-form" onSubmit={submit}>
        <fieldset className="admin-fieldset" disabled={invite.isPending}>
          <div className="admin-grid">
            <Field label="Email address">
              {(props) => (
                <input
                  {...props}
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  required
                  maxLength={320}
                  autoComplete="off"
                />
              )}
            </Field>
            <Field label="Role">
              {(props) => (
                <select
                  {...props}
                  value={role}
                  onChange={(event) => setRole(event.target.value as Role)}
                >
                  <option value="reviewer">Reviewer</option>
                  <option value="admin">Admin</option>
                </select>
              )}
            </Field>
          </div>
          {invite.isError && (
            <div className="notice danger" role="alert">
              {messageOf(invite.error, "The invitation could not be created.")}
            </div>
          )}
          <div className="admin-actions">
            <button className="button primary" type="submit">
              {invite.isPending ? "Creating…" : "Create invitation link"}
            </button>
          </div>
        </fieldset>
      </form>
      {created && (
        <div className="admin-invite-result" role="region" aria-label="New invitation link">
          <p>
            <strong>Invitation created for {created.email}</strong> as{" "}
            {ROLE_LABELS[created.role] ?? created.role}. It expires{" "}
            <LocalTime value={created.expires_at} />.
          </p>
          <p>{created.delivery_message}</p>
          {link ? (
            <>
              <Field
                label="Invitation link"
                help="Share it privately with the recipient. It works once and is shown only here, now."
              >
                {(props) => (
                  <input
                    {...props}
                    readOnly
                    value={link}
                    spellCheck={false}
                    autoComplete="off"
                    onFocus={(event) => event.currentTarget.select()}
                  />
                )}
              </Field>
              <div className="admin-actions">
                <button
                  type="button"
                  className="button primary"
                  onClick={() => void copyLink()}
                >
                  Copy link
                </button>
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => {
                    invite.reset();
                    setCopy("idle");
                  }}
                >
                  Done
                </button>
              </div>
            </>
          ) : (
            <div className="notice danger" role="alert">
              The server returned an invitation link this page can’t safely
              show. Revoke this invitation and create another.
            </div>
          )}
          <p className="admin-status" role="status">
            {copy === "copied"
              ? "Link copied to the clipboard."
              : copy === "failed"
                ? "Copying isn’t available here. Select the link above and copy it manually."
                : ""}
          </p>
        </div>
      )}
    </div>
  );
}

function UserRow({
  user,
  self,
  soleAdmin,
}: {
  user: User;
  self: boolean;
  soleAdmin: boolean;
}) {
  const update = useUpdateUser();
  const noteId = useId();
  const [role, setRole] = useState<Role>(user.role);
  const [active, setActive] = useState(user.active);
  // Follow server changes; a failed save leaves the server values, so edits stay.
  useEffect(() => {
    setRole(user.role);
    setActive(user.active);
  }, [user.role, user.active]);
  const changes: UserChanges = {};
  if (role !== user.role) changes.role = role;
  if (active !== user.active) changes.active = active;
  const dirty = Object.keys(changes).length > 0;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (dirty) update.mutate({ id: user.id, changes });
  }

  return (
    <li>
      <form onSubmit={submit} aria-label={`Access for ${user.email}`}>
        <div className="admin-user-row">
          <div className="admin-user-identity">
            <strong>
              {user.display_name}
              {self && " (you)"}
            </strong>
            <span>{user.email}</span>
          </div>
          {!user.active && <span className="badge">Inactive</span>}
          <fieldset
            className="admin-fieldset admin-user-controls"
            disabled={update.isPending}
          >
            <select
              aria-label={`Role for ${user.email}`}
              value={role}
              onChange={(event) => setRole(event.target.value as Role)}
              disabled={soleAdmin}
              aria-describedby={soleAdmin ? noteId : undefined}
            >
              <option value="reviewer">Reviewer</option>
              <option value="admin">Admin</option>
            </select>
            <label className="checkbox-label">
              <input
                type="checkbox"
                aria-label={`Active: ${user.email}`}
                checked={active}
                onChange={(event) => setActive(event.target.checked)}
                disabled={soleAdmin}
                aria-describedby={soleAdmin ? noteId : undefined}
              />
              <span aria-hidden="true">Active</span>
            </label>
            <button
              type="submit"
              className="button secondary"
              disabled={!dirty}
              aria-label={`Save changes for ${user.email}`}
            >
              {update.isPending ? "Saving…" : "Save"}
            </button>
          </fieldset>
        </div>
        {soleAdmin && (
          <p id={noteId} className="field-help admin-row-note">
            Keep at least one active Admin. Make someone else an active Admin
            before changing this account.
          </p>
        )}
        {user.active && !active && (
          <p className="admin-warning">
            Deactivating signs {self ? "you" : "this person"} out of every
            session right away. They can’t sign in again until reactivated;
            their past decisions stay attributed to them.
          </p>
        )}
        {self && dirty && (
          <p className="admin-warning">
            You’re changing your own access. Without an active Admin role you
            will leave these settings{!active ? " and be signed out" : ""}.
          </p>
        )}
        {update.isError && (
          <div className="notice danger" role="alert">
            {messageOf(update.error, "The change could not be saved.")}
          </div>
        )}
      </form>
    </li>
  );
}

function Invitations({ invitations }: { invitations: Invitation[] }) {
  const revoke = useRevokeInvitation();
  return (
    <div className="admin-subsection">
      <h3>Pending invitations</h3>
      {revoke.isError && (
        <div className="notice danger" role="alert">
          {messageOf(revoke.error, "The invitation could not be revoked.")}
        </div>
      )}
      {invitations.length === 0 ? (
        <p className="admin-muted">No pending invitations.</p>
      ) : (
        <div className="csl-table-wrap" role="region" aria-label="Pending invitations" tabIndex={0}>
          <table className="csl-table">
            <thead>
              <tr>
                <th scope="col">Email</th>
                <th scope="col">Role</th>
                <th scope="col">Created</th>
                <th scope="col">Expires</th>
                <th scope="col">
                  <span className="admin-visually-hidden">Action</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {invitations.map((invitation) => (
                <tr key={invitation.id}>
                  <td>{invitation.email}</td>
                  <td>{ROLE_LABELS[invitation.role] ?? invitation.role}</td>
                  <td>
                    <LocalTime value={invitation.created_at} />
                  </td>
                  <td>
                    <LocalTime value={invitation.expires_at} />
                  </td>
                  <td>
                    <button
                      type="button"
                      className="button secondary admin-compact"
                      onClick={() => revoke.mutate(invitation.id)}
                      disabled={revoke.isPending}
                      aria-label={`Revoke invitation for ${invitation.email}`}
                    >
                      {revoke.isPending && revoke.variables === invitation.id
                        ? "Revoking…"
                        : "Revoke"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function UsersPanel({ session }: { session: Session }) {
  const titleId = useId();
  const users = useUsers(session);
  if (!canAdminister(session)) return null;
  const items = users.data?.items ?? [];
  const activeAdmins = items.filter(
    (user) => user.active && user.role === "admin",
  ).length;

  return (
    <section className="panel settings-panel" aria-labelledby={titleId}>
      <h2 id={titleId}>People</h2>
      <p className="admin-intro">
        Invite reviewers and administrators, change roles and manage access.
        Accounts are deactivated rather than deleted, so decisions stay
        attributed.
      </p>
      <InviteForm />
      {users.data ? (
        <>
          <div className="admin-subsection">
            <h3>Users</h3>
            {users.isError && (
              <div className="notice danger" role="alert">
                The latest user list could not be loaded.{" "}
                {messageOf(users.error, "")}
              </div>
            )}
            {items.length === 0 ? (
              <p className="admin-muted">No users found.</p>
            ) : (
              <ul className="admin-user-list">
                {items.map((user) => (
                  <UserRow
                    key={user.id}
                    user={user}
                    self={user.id === session.user.id}
                    soleAdmin={
                      user.active && user.role === "admin" && activeAdmins <= 1
                    }
                  />
                ))}
              </ul>
            )}
          </div>
          <Invitations invitations={users.data.invitations} />
        </>
      ) : users.isError ? (
        <div className="admin-subsection">
          <div className="notice danger" role="alert">
            We couldn’t load users. {messageOf(users.error, "")}
          </div>
          <button
            type="button"
            className="button secondary"
            onClick={() => void users.refetch()}
            disabled={users.isFetching}
          >
            {users.isFetching ? "Trying again…" : "Try again"}
          </button>
        </div>
      ) : (
        <p className="admin-muted admin-subsection" role="status">
          Loading users…
        </p>
      )}
    </section>
  );
}
