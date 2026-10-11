"use client";

/**
 * The page an app gets when nobody wrote it one.
 *
 * Every app this build SHIPS has a hand-written page next to this file, and Next
 * resolves static segments before dynamic ones, so /dashboard/apps/calendar still
 * lands on calendar/page.tsx. This route is for the app an OWNER adds: a folder
 * under `apps/` with an `app.json` that declares a route of
 * /dashboard/apps/<id>. The nav and the apps index derive their entries from that
 * route — and until this file existed, following one was a 404. A declarative app
 * could be installed, could hold data, and could not be looked at.
 *
 * WHAT IT RENDERS, AND WHY THAT AND NOT MORE
 * ------------------------------------------
 * The backend describes an app to a browser with id, name, blurb, route, icon,
 * surfaces and whether it is degraded. So this page shows those, plus the app's
 * own data, read and written through the SAME generic /apps/<id>/data endpoints
 * the shipped pages use.
 *
 * A manifest's `vocabulary` and `actions` are not served to the browser, so this
 * page does not list them and does not pretend to. It says where those words do
 * work — in conversation, because installing an app registers its keywords with
 * the router — rather than drawing buttons that dispatch nowhere.
 *
 * The write path is the generic one: pull the document, change it, push it back
 * with PUT. That is what an app gets without anybody writing code.
 *
 * BuildBot's generic app page, rendered in this shell's components.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  mdiAlertCircleOutline,
  mdiApps,
  mdiDelete,
  mdiMessageText,
  mdiPlus,
  mdiRefresh,
  mdiTrashCanOutline,
} from "@mdi/js";
import SectionMain from "@/app/_components/Section/Main";
import SectionTitleLineWithButton from "@/app/_components/Section/TitleLineWithButton";
import CardBox from "@/app/_components/CardBox";
import Button from "@/app/_components/Button";
import Icon from "@/app/_components/Icon";
import { apiGet, apiPut } from "@/app/_lib/api";
import {
  getInstalledApps,
  type InstalledApp,
} from "@/app/_lib/services/installedAppsService";
import {
  deletePersona,
  getOwnerApps,
  personaProblem,
  type OwnerApp,
} from "@/app/_lib/services/personaService";
import { notifyBuildChanged } from "@/app/_lib/services/buildEvents";
import { CHAT_ROUTE, stashChatQuery } from "@/app/_lib/chatHandoff";

type AppDocument = Record<string, unknown>;

/** Keys the store keeps for itself. Shown nowhere; they are bookkeeping. */
const HIDDEN_KEYS = new Set(["last_updated"]);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** One value, as short text. Objects and arrays are JSON so nothing renders
 *  "[object Object]" at somebody trying to read their own data. */
function cellText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

/** The columns a list of records needs: every key any row declares, in the order
 *  they first appear. Schema-driven, because the schema is the app author's and
 *  this page has never seen it. */
function columnsOf(rows: unknown[]): string[] {
  const out: string[] = [];
  for (const row of rows) {
    if (!isPlainObject(row)) continue;
    for (const key of Object.keys(row)) {
      if (!out.includes(key)) out.push(key);
    }
  }
  return out;
}

export default function GenericAppPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const id = String(params?.id ?? "");

  const [app, setApp] = useState<InstalledApp | null>(null);
  /** This app as /persona/list describes it: its own list keys and one sentence
   *  that demonstrably works. null = not one of the owner's, or the box could
   *  not say — and either way nothing about deleting or saying is offered. */
  const [owned, setOwned] = useState<OwnerApp | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [typedId, setTypedId] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  /** true = the box told us the list; false = it could not. Not "not installed". */
  const [known, setKnown] = useState<boolean | null>(null);
  const [doc, setDoc] = useState<AppDocument | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [addTo, setAddTo] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);

  const loadDoc = useCallback(async () => {
    if (!id) return;
    try {
      setDoc(await apiGet<AppDocument>(`/apps/${id}/data`));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "the store did not answer");
    }
  }, [id]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      // The box answers what this app IS; the store answers what it HOLDS. They
      // are independent: an app can be installed with nothing in it, and a box
      // that cannot list its apps can still serve the store.
      const apps = await getInstalledApps();
      if (cancelled) return;
      if (apps) {
        setKnown(true);
        setApp(apps.find((entry) => entry.id === id) ?? null);
      } else {
        setKnown(false);
        setApp(null);
      }
      // A third, independent question: is this one the OWNER made? Only those
      // carry Say hints and a Delete, and only the box can answer it — the
      // manifest does not record which root it came from.
      const mine = await getOwnerApps();
      if (!cancelled) {
        setOwned(mine?.find((entry) => entry.id === id) ?? null);
      }
      await loadDoc();
      if (!cancelled) setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [id, loadDoc]);

  /** Hand one sentence to the chat page and go there. */
  const say = useCallback(
    (phrase: string) => {
      if (stashChatQuery(phrase)) router.push(CHAT_ROUTE);
    },
    [router],
  );

  const removePersona = useCallback(async () => {
    setDeleting(true);
    try {
      await deletePersona(id, typedId.trim());
      notifyBuildChanged();
      router.push("/dashboard/apps");
    } catch (err) {
      setDeleteError(personaProblem(err));
      setDeleting(false);
    }
  }, [id, router, typedId]);

  /** The document's top-level entries, split into the ones worth a table and the
   *  ones that are a single value. */
  const sections = useMemo(() => {
    const lists: { key: string; rows: unknown[] }[] = [];
    const fields: { key: string; value: unknown }[] = [];
    for (const [key, value] of Object.entries(doc ?? {})) {
      if (HIDDEN_KEYS.has(key)) continue;
      if (Array.isArray(value)) lists.push({ key, rows: value });
      else fields.push({ key, value });
    }
    return { lists, fields };
  }, [doc]);

  /** Pull, change, push — on the document already held rather than a fresh read,
   *  so two tabs cannot silently merge into each other unnoticed. */
  const writeList = useCallback(
    async (key: string, rows: unknown[]) => {
      if (!doc) return;
      setSaving(true);
      try {
        const next = { ...doc, [key]: rows };
        await apiPut(`/apps/${id}/data`, next);
        setDoc(next);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "the store refused the write");
        await loadDoc();
      } finally {
        setSaving(false);
      }
    },
    [doc, id, loadDoc],
  );

  const addItem = useCallback(
    async (key: string) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(draft);
      } catch {
        setDraftError('That is not JSON. One object, e.g. {"name": "bench 2"}');
        return;
      }
      if (!isPlainObject(parsed) && typeof parsed !== "string" && typeof parsed !== "number") {
        setDraftError("An item has to be one JSON object, string or number.");
        return;
      }
      setDraftError(null);
      const rows = Array.isArray(doc?.[key]) ? (doc[key] as unknown[]) : [];
      await writeList(key, [...rows, parsed]);
      setDraft("");
      setAddTo(null);
    },
    [doc, draft, writeList],
  );

  if (loading) {
    return (
      <SectionMain>
        <div className="flex items-center justify-center py-20">
          <div className="h-10 w-10 animate-spin rounded-full border-4 border-[#00B4D8] border-t-transparent" />
        </div>
      </SectionMain>
    );
  }

  // Known list, and this id is not on it. Say so plainly, and say how it is
  // fixed, rather than showing an empty page that reads like a broken one.
  if (known === true && !app) {
    return (
      <SectionMain>
        <CardBox className="mb-6">
          <div className="flex items-start gap-3 p-5">
            <Icon path={mdiAlertCircleOutline} size="20" className="mt-0.5 text-amber-500" />
            <div>
              <p className="font-semibold text-[#0C2F44] dark:text-white">
                No app called “{id}” is installed on this build.
              </p>
              <p className="mt-1 text-sm text-gray-600 dark:text-slate-400">
                An app is a folder in <code>apps/</code> holding an{" "}
                <code>app.json</code>. Drop one in and rescan (POST{" "}
                <code>/apps/rescan</code>, or restart the backend) and it will
                appear here.
              </p>
            </div>
          </div>
        </CardBox>
      </SectionMain>
    );
  }

  const title = app?.name || id;

  return (
    <SectionMain>
      <SectionTitleLineWithButton icon={mdiApps} title={title} main>
        <Button
          icon={mdiRefresh}
          label="Reload"
          color="info"
          small
          outline
          onClick={loadDoc}
        />
      </SectionTitleLineWithButton>

      {/* ── Header: the app's own words, rendered as text ── */}
      <CardBox className="mb-6">
        <div className="p-5">
          {app?.blurb && (
            <p className="text-sm text-gray-700 dark:text-slate-300">{app.blurb}</p>
          )}
          {(app?.surfaces?.length ?? 0) > 0 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {app!.surfaces!.map((surface) => (
                <span
                  key={surface}
                  className="rounded-full bg-gray-100 px-2.5 py-1 font-mono text-[11px] font-semibold text-gray-600 dark:bg-slate-700 dark:text-slate-300"
                >
                  {surface}
                </span>
              ))}
            </div>
          )}
          {app?.degraded && app.degraded_reason && (
            <p className="mt-3 border-l-[3px] border-amber-400 pl-3 text-sm font-semibold text-amber-600 dark:text-amber-400">
              {app.degraded_reason}
            </p>
          )}
          {known === false && (
            /* The list call failed. Say that rather than implying the app is
               fine or that it is missing — this page still shows its store. */
            <p className="mt-3 text-xs text-gray-500 dark:text-slate-400">
              This box did not say which apps it has installed, so only the stored
              data below is certain.
            </p>
          )}
          <p className="mt-3 text-xs tracking-wide text-gray-500 uppercase dark:text-slate-400">
            Generic page — nobody wrote {title} one of its own. Below is its
            declared data, from the store this build gave it.
          </p>
        </div>
      </CardBox>

      {error && (
        <CardBox className="mb-6">
          <div className="flex items-center gap-2 p-4 text-sm text-red-600 dark:text-red-400">
            <Icon path={mdiAlertCircleOutline} size="20" />
            <span>{error}</span>
          </div>
        </CardBox>
      )}

      {/* ── The store ── */}
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-[#0C2F44] dark:text-white">
          What this app holds
        </h2>
        <span className="font-mono text-[11px] text-gray-400">/apps/{id}/data</span>
      </div>

      {sections.fields.length > 0 && (
        <CardBox className="mb-6">
          <table className="w-full text-left text-sm">
            <tbody className="divide-y divide-gray-100 dark:divide-slate-700">
              {sections.fields.map((field) => (
                <tr key={field.key}>
                  <td className="w-1/3 px-4 py-2 font-mono text-xs text-gray-500 dark:text-slate-400">
                    {field.key}
                  </td>
                  <td className="px-4 py-2 text-gray-800 dark:text-slate-200">
                    {cellText(field.value)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardBox>
      )}

      {sections.lists.length === 0 && sections.fields.length === 0 && (
        <CardBox className="mb-6">
          <p className="py-6 text-center text-sm text-gray-500 dark:text-slate-400">
            This app declared no data shape, so there is nothing to show yet.
          </p>
        </CardBox>
      )}

      {sections.lists.map(({ key, rows }) => {
        const columns = columnsOf(rows);
        return (
          <div key={key} className="mb-6">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="font-mono text-sm font-semibold text-[#0C2F44] dark:text-white">
                {key}
                <span className="ml-2 font-sans text-xs font-normal text-gray-500 dark:text-slate-400">
                  {rows.length} {rows.length === 1 ? "item" : "items"}
                </span>
              </h3>
              <Button
                icon={mdiPlus}
                label="Add item"
                color="info"
                small
                outline
                disabled={saving}
                onClick={() => {
                  setAddTo(addTo === key ? null : key);
                  setDraft("");
                  setDraftError(null);
                }}
              />
            </div>

            {addTo === key && (
              <CardBox className="mb-2">
                <div className="p-4">
                  <p className="mb-2 text-xs text-gray-600 dark:text-slate-400">
                    One item, as JSON. This page has never seen {title}&apos;s
                    schema, so it does not invent fields for you — it appends what
                    you type and saves the whole document back.
                  </p>
                  <textarea
                    className="w-full rounded-lg border border-gray-200 bg-white/70 p-2 font-mono text-xs dark:border-slate-600 dark:bg-slate-800/70 dark:text-white"
                    rows={3}
                    spellCheck={false}
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    placeholder={'{"name": "bench 2", "moisture": 31}'}
                  />
                  {draftError && (
                    <p className="mt-1 text-xs text-red-600 dark:text-red-400">{draftError}</p>
                  )}
                  <div className="mt-2 flex gap-2">
                    <Button
                      label="Save"
                      color="info"
                      small
                      disabled={saving}
                      onClick={() => addItem(key)}
                    />
                    <Button
                      label="Cancel"
                      color="white"
                      small
                      outline
                      onClick={() => setAddTo(null)}
                    />
                  </div>
                </div>
              </CardBox>
            )}

            {rows.length === 0 ? (
              <CardBox>
                <p className="py-4 text-center text-sm text-gray-500 dark:text-slate-400">
                  Nothing in {key} yet.
                </p>
              </CardBox>
            ) : (
              <CardBox>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead>
                      <tr className="border-b border-gray-100 dark:border-slate-700">
                        {(columns.length > 0 ? columns : ["value"]).map((column) => (
                          <th
                            key={column}
                            className="px-4 py-2 font-mono text-xs font-semibold text-gray-500 dark:text-slate-400"
                          >
                            {column}
                          </th>
                        ))}
                        <th className="w-10 px-4 py-2" />
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100 dark:divide-slate-700">
                      {rows.map((row, index) => (
                        <tr key={index}>
                          {columns.length > 0 ? (
                            columns.map((column) => (
                              <td
                                key={column}
                                className="px-4 py-2 text-gray-800 dark:text-slate-200"
                              >
                                {isPlainObject(row) ? cellText(row[column]) : ""}
                              </td>
                            ))
                          ) : (
                            <td className="px-4 py-2 text-gray-800 dark:text-slate-200">
                              {cellText(row)}
                            </td>
                          )}
                          <td className="px-4 py-2 text-right">
                            <button
                              type="button"
                              aria-label={`Remove item ${index + 1} from ${key}`}
                              disabled={saving}
                              className="text-gray-400 transition hover:text-red-500 disabled:opacity-40"
                              onClick={() =>
                                writeList(
                                  key,
                                  rows.filter((_, position) => position !== index),
                                )
                              }
                            >
                              <Icon path={mdiDelete} size="16" />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </CardBox>
            )}
          </div>
        );
      })}

      {/* ── What the manifest's words actually do ── */}
      <h2 className="mb-2 text-lg font-semibold text-[#0C2F44] dark:text-white">
        Saying it out loud
      </h2>

      {owned && owned.lists.length > 0 && (
        /* Only for an app the OWNER made, and only from the box's own answer:
           `try` is the backend's sentence verbatim, and the read alongside it is
           built from the manifest's own list labels, which /persona/list served.
           Nothing here is guessed from the name typed into the builder. */
        <CardBox className="mb-3">
          <div className="p-5">
            <p className="mb-3 text-sm text-gray-700 dark:text-slate-300">
              These work now, with no language model involved — the list verbs are
              answered from {title}&apos;s own store. Click one to say it in chat.
            </p>
            <div className="flex flex-wrap gap-2">
              {[
                owned.try,
                ...owned.list_labels.map((label) => `what is on the ${label} list`),
              ]
                .filter(Boolean)
                .map((phrase) => (
                  <button
                    key={phrase}
                    type="button"
                    onClick={() => say(phrase)}
                    className="inline-flex cursor-pointer items-center gap-1.5 rounded-full bg-gray-100 px-3 py-1.5 text-xs font-semibold text-gray-700 transition hover:bg-gray-200 dark:bg-slate-700 dark:text-slate-200 dark:hover:bg-slate-600"
                  >
                    <Icon path={mdiMessageText} size="14" />
                    &ldquo;{phrase}&rdquo;
                  </button>
                ))}
            </div>
          </div>
        </CardBox>
      )}

      <CardBox className="mb-6">
        <div className="p-5 text-sm text-gray-700 dark:text-slate-300">
          <p>
            Installing {title} registered the <code>vocabulary.keywords</code> in
            its manifest with the router, so its phrases are understood in chat.
            The <code>actions</code> it declares are phrases too — they are not
            buttons: nothing in a manifest can add a method to this build, so
            drawing one here would be inviting a click that does nothing.
          </p>
          <p className="mt-2">
            This page cannot list them, either. The backend hands a browser only
            an app&apos;s id, name, blurb, route, icon and surfaces; its words stay
            on the box, in <code>apps/{id}/app.json</code>.
          </p>
        </div>
      </CardBox>

      {owned && (
        /* Offered ONLY for an app the owner made — the box decides that, by
           reading which directory the manifest came out of, and it refuses again
           on the DELETE itself. A shipped app is removed by changing the build.

           The id is typed back rather than clicked through. A confirm dialog is
           one keystroke from yes; typing the name is the owner saying which one,
           and it is the same gate the backend enforces. */
        <>
          <h2 className="mb-2 text-lg font-semibold text-[#0C2F44] dark:text-white">
            Delete it
          </h2>
          <CardBox className="mb-6">
            <div className="p-5">
              <p className="text-sm text-gray-700 dark:text-slate-300">
                This removes {title}&apos;s app folder, its pack and everything in
                its store. Nothing else on this build is touched, and there is no
                undo — the files are on this box, not in a bin.
              </p>
              {!confirmDelete ? (
                <div className="mt-3">
                  <Button
                    icon={mdiTrashCanOutline}
                    label="Delete this app"
                    color="danger"
                    small
                    outline
                    onClick={() => {
                      setConfirmDelete(true);
                      setTypedId("");
                      setDeleteError(null);
                    }}
                  />
                </div>
              ) : (
                <div className="mt-3">
                  <label
                    htmlFor="persona-confirm"
                    className="block text-xs font-semibold tracking-wide text-gray-500 uppercase dark:text-slate-400"
                  >
                    Type <code>{id}</code> to confirm
                  </label>
                  <input
                    id="persona-confirm"
                    value={typedId}
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(event) => setTypedId(event.target.value)}
                    className="mt-1.5 w-full max-w-xs rounded-lg border border-gray-200 bg-white/70 p-2 font-mono text-sm dark:border-slate-600 dark:bg-slate-800/70 dark:text-white"
                  />
                  {deleteError && (
                    <p className="mt-2 text-sm text-red-600 dark:text-red-400">
                      {deleteError}
                    </p>
                  )}
                  <div className="mt-3 flex gap-2">
                    <Button
                      icon={mdiTrashCanOutline}
                      label={deleting ? "Deleting…" : "Delete"}
                      color="danger"
                      small
                      disabled={deleting || typedId.trim() !== id}
                      onClick={removePersona}
                    />
                    <Button
                      label="Cancel"
                      color="white"
                      small
                      outline
                      disabled={deleting}
                      onClick={() => {
                        setConfirmDelete(false);
                        setDeleteError(null);
                      }}
                    />
                  </div>
                </div>
              )}
            </div>
          </CardBox>
        </>
      )}
    </SectionMain>
  );
}
