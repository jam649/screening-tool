import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Home } from "./Home";
import { ReviewQueue } from "./ReviewQueue";
import { Parties, PartyFile } from "./Parties";
import { Audit } from "./Audit";
import { CSLPanel } from "./CSLPanel";
import { Sources } from "./Sources";
import { QboSettings } from "./QboSettings";
import { BcSettings } from "./BcSettings";
import { NotificationSettings, NotificationsBanner } from "./NotificationSettings";
import { MonitoringPanel, MonitoringSummary } from "./MonitoringPanel";
import { AutoClearControls } from "./AutoClearControls";
import { RetentionControls } from "./RetentionControls";
import { SettingsControls } from "./SettingsControls";
import { UsersPanel } from "./UsersPanel";
import { AcceptInvite } from "./AcceptInvite";
import "./admin.css";
import "./sources.css";
import "./csl.css";
import {
  api,
  sessionQuery,
  setSession,
  setupQuery,
  type Session,
  type SetupStatus,
} from "./api";

function ErrorMessage({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div className="notice danger" role="alert">
      {error instanceof Error
        ? error.message
        : "Something went wrong. Please try again."}
    </div>
  );
}

function Brand({ name }: { name: string }) {
  return (
    <span className="brand">
      <span className="brand-symbol" aria-hidden="true">
        s/
      </span>
      {name}
    </span>
  );
}

function Centered({ children }: { children: ReactNode }) {
  return (
    <main className="centered">
      <div className="centered-content">{children}</div>
    </main>
  );
}

function Authentication({ setup }: { setup: SetupStatus }) {
  const client = useQueryClient();
  const [step, setStep] = useState(1);
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const isSetup = setup.needs_setup;
  const mutation = useMutation({
    mutationFn: () =>
      api<Session>(isSetup ? "/setup" : "/auth/login", {
        method: "POST",
        body: JSON.stringify(
          isSetup
            ? {
                email,
                display_name: displayName,
                password,
                disclaimer_acknowledged: acknowledged,
              }
            : { email, password },
        ),
      }),
    onSuccess: (session) => {
      setSession(session);
      client.setQueryData(["session"], session);
      client.setQueryData(["setup"], { ...setup, needs_setup: false });
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (isSetup && step === 1) {
      setStep(2);
      return;
    }
    mutation.mutate();
  }
  return (
    <main className="auth-layout">
      <aside className="auth-aside">
        <Brand name={setup.product_name} />
        <div className="auth-statement">
          <span className="eyebrow light">A dependable daily practice</span>
          <h1>
            Know who you
            <br />
            do business with.
          </h1>
          <p>
            Screen your customers and vendors. Review possible matches. Keep a
            clear record of every decision.
          </p>
        </div>
        <div className="auth-footnote">
          <span className="status-dot" /> Self-hosted. Built for human review.
        </div>
      </aside>
      <section
        className="auth-main"
        aria-label={isSetup ? "Workspace setup" : "Sign in"}
      >
        <div className="auth-form-wrap">
          <div className="eyebrow">
            {isSetup ? "Welcome to your workspace" : "Your screening workspace"}
          </div>
          <h2>
            {isSetup
              ? step === 1
                ? "Let’s get you set up."
                : "A shared understanding."
              : "Welcome back."}
          </h2>
          <p className="lede">
            {isSetup
              ? step === 1
                ? "Create the first administrator account to get started. You can add reviewers later."
                : "Before you begin, please read the International Trade Administration’s notice about using the Consolidated Screening List."
              : "Sign in to review your screening activity."}
          </p>
          {isSetup && (
            <ol className="steps" aria-label="Setup progress">
              <li className={step === 1 ? "current" : "complete"}>
                <span>{step > 1 ? "✓" : "1"}</span> Your account
              </li>
              <li className={step === 2 ? "current" : ""}>
                <span>2</span> Using the CSL
              </li>
            </ol>
          )}
          <form onSubmit={submit}>
            {(!isSetup || step === 1) && (
              <div className="form-fields">
                {isSetup && (
                  <label>
                    Full name
                    <input
                      autoComplete="name"
                      value={displayName}
                      onChange={(event) => setDisplayName(event.target.value)}
                      required
                      maxLength={120}
                      autoFocus
                    />
                  </label>
                )}
                <label>
                  Email address
                  <input
                    type="email"
                    autoComplete="username"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    required
                    maxLength={254}
                    autoFocus={!isSetup}
                  />
                </label>
                <label>
                  Password
                  <input
                    type="password"
                    aria-label="Password"
                    autoComplete={isSetup ? "new-password" : "current-password"}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    required
                    minLength={isSetup ? 12 : undefined}
                    maxLength={1024}
                    aria-describedby={isSetup ? "password-help" : undefined}
                  />
                  {isSetup && (
                    <span id="password-help" className="field-help">
                      Use at least 12 characters. A few memorable words work
                      well.
                    </span>
                  )}
                </label>
              </div>
            )}
            {isSetup && step === 2 && (
              <>
                <blockquote className="disclaimer">
                  {setup.disclaimer}
                </blockquote>
                <a
                  className="text-link"
                  href={setup.disclaimer_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Read the ITA guidance <span aria-hidden="true">↗</span>
                </a>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={acknowledged}
                    onChange={(event) => setAcknowledged(event.target.checked)}
                    required
                  />
                  <span>I have read and acknowledge this notice.</span>
                </label>
              </>
            )}
            <ErrorMessage error={mutation.error} />
            <div className="form-actions">
              {isSetup && step === 2 && (
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => setStep(1)}
                  disabled={mutation.isPending}
                >
                  Back
                </button>
              )}
              <button
                className="button primary"
                type="submit"
                disabled={
                  mutation.isPending || (isSetup && step === 2 && !acknowledged)
                }
              >
                {mutation.isPending
                  ? "Please wait…"
                  : isSetup
                    ? step === 1
                      ? "Continue"
                      : "Create workspace"
                    : "Sign in"}
                <span aria-hidden="true">→</span>
              </button>
            </div>
          </form>
          <p className="auth-note">
            {isSetup
              ? "Your account will have administrator access to this installation."
              : "Need access? Contact your workspace administrator."}
          </p>
        </div>
        <footer className="auth-footer">
          Built for a thoughtful compliance process.
        </footer>
      </section>
    </main>
  );
}

function Settings({
  setup,
  session,
}: {
  setup: SetupStatus;
  session: Session;
}) {
  return (
    <>
      <header className="page-header">
        <div>
          <div className="eyebrow">Workspace</div>
          <h1>Settings</h1>
          <p className="lede">
            Your account and the terms that guide this workspace.
          </p>
        </div>
      </header>
      <section className="panel settings-panel">
        <h2>Your account</h2>
        <dl className="detail-list">
          <div>
            <dt>Name</dt>
            <dd>{session.user.display_name}</dd>
          </div>
          <div>
            <dt>Email</dt>
            <dd>{session.user.email}</dd>
          </div>
          <div>
            <dt>Role</dt>
            <dd className="capitalize">{session.user.role}</dd>
          </div>
        </dl>
      </section>
      <SettingsControls session={session} />
      <AutoClearControls session={session} />
      <MonitoringPanel session={session} />
      <NotificationSettings session={session} />
      <QboSettings session={session} />
      <BcSettings session={session} />
      <RetentionControls session={session} />
      <UsersPanel session={session} />
      <CSLPanel />
      <section className="panel settings-panel">
        <h2>Using the Consolidated Screening List</h2>
        <blockquote className="disclaimer">{setup.disclaimer}</blockquote>
        <a
          className="text-link"
          href={setup.disclaimer_url}
          target="_blank"
          rel="noopener noreferrer"
        >
          International Trade Administration guidance ↗
        </a>
      </section>
    </>
  );
}

function Workspace({
  setup,
  session,
}: {
  setup: SetupStatus;
  session: Session;
}) {
  const client = useQueryClient();
  const logout = useMutation({
    mutationFn: () => api<void>("/auth/logout", { method: "POST" }),
    onSuccess: async () => {
      await client.cancelQueries();
      setSession(null);
      client.removeQueries({
        predicate: (query) =>
          !["setup", "session"].includes(String(query.queryKey[0])),
      });
      client.setQueryData(["session"], null);
    },
  });
  return (
    <div className="workspace">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <aside className="sidebar">
        <Link className="brand-link" to="/" aria-label="Home">
          <Brand name={setup.product_name} />
        </Link>
        <div className="sidebar-label">Workspace</div>
        <nav aria-label="Main navigation">
          <NavLink to="/" end>
            <span aria-hidden="true">◫</span>Overview
          </NavLink>
          <NavLink to="/queue"><span aria-hidden="true">≋</span>Review queue</NavLink>
          <NavLink to="/parties"><span aria-hidden="true">▦</span>Parties</NavLink>
          <NavLink to="/audit"><span aria-hidden="true">◷</span>Audit</NavLink>
          <NavLink to="/sources">
            <span aria-hidden="true">▤</span>Sources
          </NavLink>
          {session.user.role.toLowerCase() === "admin" && (
            <NavLink to="/settings">
              <span aria-hidden="true">⚙</span>Settings
            </NavLink>
          )}
        </nav>
        <div className="sidebar-bottom">
          <span className="eyebrow light">Screen with care.</span>
          <p>
            Every record.
            <br />
            Every decision.
          </p>
          <span className="sidebar-caption">Self-hosted workspace</span>
        </div>
      </aside>
      <div className="workspace-main">
        <header className="topbar">
          <span className="topbar-context">Customer & vendor screening</span>
          <div className="user-menu">
            <span className="avatar" aria-hidden="true">
              {session.user.display_name.slice(0, 1).toUpperCase()}
            </span>
            <div>
              <strong>{session.user.display_name}</strong>
              <span className="capitalize">{session.user.role}</span>
            </div>
            <button
              className="button text"
              onClick={() => logout.mutate()}
              disabled={logout.isPending}
            >
              Sign out
            </button>
          </div>
        </header>
        <main id="main-content" className="page-content">
          <ErrorMessage error={logout.error} />
          <NotificationsBanner session={session} />
          <Routes>
            <Route path="/" element={<><Home session={session} /><MonitoringSummary session={session} /></>} />
            <Route path="/queue" element={<ReviewQueue />} />
            <Route path="/parties" element={<Parties />} />
            <Route path="/parties/:id" element={<PartyFile session={session} />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="/sources" element={<Sources session={session} />} />
            <Route
              path="/settings"
              element={
                session.user.role.toLowerCase() === "admin" ? (
                  <Settings setup={setup} session={session} />
                ) : (
                  <Navigate to="/" replace />
                )
              }
            />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
        <footer className="workspace-footer">
          A screening result is one part of your due diligence.
        </footer>
      </div>
    </div>
  );
}

export function App() {
  const location = useLocation();
  const acceptingInvite = location.pathname === "/accept-invite";
  const setup = useQuery({ queryKey: ["setup"], queryFn: setupQuery });
  const session = useQuery({
    queryKey: ["session"],
    queryFn: sessionQuery,
    enabled: setup.data?.needs_setup === false && !acceptingInvite,
  });
  useEffect(() => {
    if (setup.data) document.title = setup.data.product_name;
  }, [setup.data]);
  if (acceptingInvite && setup.data && !setup.data.needs_setup)
    return <AcceptInvite setup={setup.data} />;
  if (
    setup.isPending ||
    (!acceptingInvite && !setup.data?.needs_setup && session.isPending && !setup.error)
  )
    return (
      <Centered>
        <span className="eyebrow">Your screening workspace</span>
        <h1>Opening workspace…</h1>
        <p role="status">Please wait while we connect.</p>
      </Centered>
    );
  if (setup.error || session.error)
    return (
      <Centered>
        <h1>We couldn’t open your workspace.</h1>
        <ErrorMessage error={setup.error ?? session.error} />
        <button
          className="button primary"
          onClick={() => {
            void setup.refetch();
            void session.refetch();
          }}
        >
          Try again
        </button>
      </Centered>
    );
  if (!setup.data) return null;
  if (setup.data.needs_setup || !session.data)
    return <Authentication setup={setup.data} />;
  return <Workspace setup={setup.data} session={session.data} />;
}
