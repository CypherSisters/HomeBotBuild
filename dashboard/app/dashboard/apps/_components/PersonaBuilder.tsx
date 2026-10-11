"use client";

/**
 * Build a persona — the dashboard half of `puffpai persona new`.
 *
 * WHAT THIS IS FOR
 * ----------------
 * The build interview is honest about a goal nothing ships for: "Nothing we
 * ship matches that yet." The useful next move is not a shopping list, it is a
 * blank app with the owner's own words in it — their lists, their name, its own
 * page. The CLI has written one since the scaffold landed. This is that, for
 * somebody who never opens a terminal.
 *
 * THE PREVIEW IS THE BACKEND'S, NOT OURS
 * --------------------------------------
 * "The words it will understand" is computed by POST /persona/preview, which
 * runs the same functions the write runs. Deriving it here — lowercase the
 * name, split the goal, drop the filler words — would be a second copy of rules
 * that live in Python, and the first time the two drifted this page would be
 * promising vocabulary the installed manifest does not carry. That is the exact
 * class of lie the whole feature exists to stop telling, so the preview costs a
 * round trip and is worth it.
 *
 * NOTHING IS WRITTEN UNTIL CREATE
 * -------------------------------
 * Preview writes nothing; the id is not reserved; a refusal is shown in the
 * backend's own words because those words were written for the owner. Create
 * scaffolds, rescans on the box, tells the mounted app lists that the build
 * changed, and lands on the app's own page.
 *
 * BuildBot's PersonaBuilder in this shell. The DATA contract is BuildBot's; the
 * chrome is this shell's — SectionMain / CardBox / Button / Icon, the same
 * components every other page here uses.
 *
 * Copyright (c) Cypher Sisters. Apache-2.0 License.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  mdiAlertCircleOutline,
  mdiClose,
  mdiPlus,
  mdiRobotOutline,
} from "@mdi/js";
import SectionMain from "@/app/_components/Section/Main";
import SectionTitleLineWithButton from "@/app/_components/Section/TitleLineWithButton";
import CardBox from "@/app/_components/CardBox";
import Button from "@/app/_components/Button";
import Icon from "@/app/_components/Icon";
import {
  personaProblem,
  previewPersona,
  scaffoldPersona,
  type PersonaPreview,
} from "@/app/_lib/services/personaService";
import { notifyBuildChanged } from "@/app/_lib/services/buildEvents";

/** Matches the backend's ceilings (persona_scaffold). Duplicated only as an
 *  input `maxLength`, which is a courtesy; the refusal is still the box's. */
const MAX_NAME = 40;
const MAX_LIST_NAME = 32;
const MAX_LISTS = 8;
const MAX_GOAL = 300;

/** How long to wait after a keystroke before asking the box what it would
 *  write. Long enough that typing a name is one request, not eleven. */
const PREVIEW_DEBOUNCE_MS = 400;

const inputClass =
  "w-full rounded-lg border border-gray-200 bg-white/70 p-2 text-sm " +
  "dark:border-slate-600 dark:bg-slate-800/70 dark:text-white";

function Label({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-2 text-xs font-semibold tracking-wide text-gray-500 uppercase dark:text-slate-400">
      {children}
    </div>
  );
}

function Hint({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-1.5 text-xs text-gray-500 dark:text-slate-400">{children}</p>
  );
}

function Warn({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-3 flex items-start gap-1.5 border-l-[3px] border-amber-400 pl-3 text-sm font-semibold text-amber-600 dark:text-amber-400">
      <Icon path={mdiAlertCircleOutline} size="16" className="mt-px shrink-0" />
      <span>{children}</span>
    </p>
  );
}

function Chip({
  children,
  onRemove,
  removeLabel,
}: {
  children: React.ReactNode;
  onRemove?: () => void;
  removeLabel?: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-gray-100 px-3 py-1 font-mono text-[11px] font-semibold text-gray-600 dark:bg-slate-700 dark:text-slate-300">
      {children}
      {onRemove && (
        <button
          type="button"
          aria-label={removeLabel}
          onClick={onRemove}
          className="cursor-pointer border-0 bg-transparent p-0 leading-none text-gray-400 hover:text-red-500"
        >
          <Icon path={mdiClose} size="14" />
        </button>
      )}
    </span>
  );
}

function Well({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mt-3 rounded-lg bg-gray-50 p-3 dark:bg-slate-800/60">
      <div className="text-xs font-semibold tracking-wide text-gray-500 uppercase dark:text-slate-400">
        {title}
      </div>
      {children}
    </div>
  );
}

export default function PersonaBuilder() {
  const router = useRouter();

  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [lists, setLists] = useState<string[]>([]);
  const [listDraft, setListDraft] = useState("");

  const [preview, setPreview] = useState<PersonaPreview | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const addList = useCallback(() => {
    const value = listDraft.trim();
    if (!value) return;
    setLists((current) =>
      current.length >= MAX_LISTS ||
      current.some((entry) => entry.toLowerCase() === value.toLowerCase())
        ? current
        : [...current, value],
    );
    setListDraft("");
  }, [listDraft]);

  const dropList = (value: string) =>
    setLists((current) => current.filter((entry) => entry !== value));

  // Live preview. Cancelled on every change, and a stale answer is discarded by
  // sequence number rather than by time: two requests in flight must not be able
  // to paint in the wrong order.
  const seq = useRef(0);
  useEffect(() => {
    if (!name.trim()) {
      setPreview(null);
      setProblem(null);
      return;
    }
    const mine = ++seq.current;
    const timer = setTimeout(() => {
      previewPersona({ name: name.trim(), goal: goal.trim(), lists })
        .then((data) => {
          if (seq.current !== mine) return;
          setPreview(data);
          setProblem(null);
        })
        .catch((err) => {
          if (seq.current !== mine) return;
          setPreview(null);
          setProblem(personaProblem(err));
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [goal, lists, name]);

  const create = useCallback(async () => {
    if (!name.trim() || creating) return;
    setCreating(true);
    try {
      const reply = await scaffoldPersona({
        id: preview?.id,
        name: name.trim(),
        goal: goal.trim(),
        lists,
      });
      // The box has already rescanned. Tell the apps index and the home board's
      // Apps tile, which are mounted elsewhere and would otherwise keep the list
      // they fetched before this page wrote a file.
      notifyBuildChanged();
      router.push(reply.persona.route || `/dashboard/apps/${reply.persona.id}`);
    } catch (err) {
      setProblem(personaProblem(err));
      setCreating(false);
    }
  }, [creating, goal, lists, name, preview?.id, router]);

  const blocked = Boolean(preview && (preview.taken || preview.pack_taken));
  const ready = Boolean(preview && !blocked && !creating);

  return (
    <SectionMain>
      <SectionTitleLineWithButton icon={mdiRobotOutline} title="Build a persona" main>
        <Button
          href="/dashboard/apps"
          label="Back to apps"
          color="white"
          small
          outline
          asAnchor
        />
      </SectionTitleLineWithButton>

      <p className="mb-5 text-sm text-gray-600 dark:text-slate-400">
        Name it, say in one sentence what it is for, and name the lists you want
        it to keep. That is an app: it gets its own page, its own store on this
        box, and the words below start working in chat.
      </p>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* ── what the owner says ── */}
        <CardBox>
          <div className="p-5">
            <Label>What is it called</Label>
            <input
              id="persona-name"
              value={name}
              maxLength={MAX_NAME}
              onChange={(event) => setName(event.target.value)}
              placeholder="GardenBot"
              className={inputClass}
            />
            <Hint>
              {preview ? (
                <>
                  Its id will be <code>{preview.id}</code>, and its page{" "}
                  <code>{preview.route}</code>.
                </>
              ) : (
                <>Up to {MAX_NAME} characters. The id is made from it.</>
              )}
            </Hint>

            <div className="mt-5">
              <Label>What is it for</Label>
              <textarea
                id="persona-goal"
                value={goal}
                rows={3}
                maxLength={MAX_GOAL}
                onChange={(event) => setGoal(event.target.value)}
                placeholder="a garden and greenhouse assistant for a small family farm"
                className={inputClass}
              />
              <Hint>
                One sentence. This is data: it becomes the line on the app&apos;s
                page and a few words for the router to match. It is never read as
                an instruction to the model.
              </Hint>
            </div>

            <div className="mt-5">
              <Label>Lists it should keep</Label>
              <div className="mb-2.5 flex flex-wrap gap-2">
                {lists.map((entry) => (
                  <Chip
                    key={entry}
                    onRemove={() => dropList(entry)}
                    removeLabel={`Remove the ${entry} list`}
                  >
                    {entry}
                  </Chip>
                ))}
                {lists.length === 0 && (
                  <span className="text-xs tracking-wide text-gray-400 uppercase">
                    none yet
                  </span>
                )}
              </div>
              <div className="flex gap-2">
                <input
                  id="persona-list"
                  value={listDraft}
                  maxLength={MAX_LIST_NAME}
                  onChange={(event) => setListDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      addList();
                    }
                  }}
                  placeholder="plantings"
                  className={inputClass}
                  disabled={lists.length >= MAX_LISTS}
                />
                <Button
                  icon={mdiPlus}
                  label="Add"
                  color="info"
                  small
                  outline
                  disabled={!listDraft.trim() || lists.length >= MAX_LISTS}
                  onClick={addList}
                />
              </div>
              <Hint>
                {lists.length >= MAX_LISTS
                  ? `${MAX_LISTS} lists is as many as one app carries.`
                  : `Up to ${MAX_LISTS}, each up to ${MAX_LIST_NAME} characters. An app with no lists is still a valid app.`}
              </Hint>
            </div>
          </div>
        </CardBox>

        {/* ── what the box says it would write ── */}
        <CardBox>
          <div className="p-5">
            <h2 className="mb-3 text-lg font-semibold text-[#0C2F44] dark:text-white">
              What it will understand
            </h2>

            {!name.trim() && (
              <p className="text-sm text-gray-600 dark:text-slate-400">
                Give it a name and this fills in. Every word here comes from the
                box, from the same code that writes the file — so what you read
                is what gets installed.
              </p>
            )}

            {problem && <Warn>{problem}</Warn>}

            {preview && (
              <>
                {preview.taken && (
                  <Warn>
                    An app called {preview.id} is already installed. Pick another
                    name — nothing of yours is overwritten.
                  </Warn>
                )}
                {!preview.taken && preview.pack_taken && (
                  /* This build ships a pack called "homebot" that no app holds.
                     Said here, before Create, rather than as a refusal after. */
                  <Warn>
                    A pack called {preview.id} is already installed on this
                    build, and two packs may not claim one id. Try{" "}
                    <code>{preview.suggested_id}</code> instead.
                  </Warn>
                )}

                <Well title="Say">
                  <ul className="mt-2 list-none space-y-1 p-0 text-sm text-gray-800 dark:text-slate-200">
                    {(preview.says.length > 0 ? preview.says : preview.examples)
                      .slice(0, 6)
                      .map((phrase) => (
                        <li key={phrase}>&ldquo;{phrase}&rdquo;</li>
                      ))}
                    {preview.says.length === 0 &&
                      preview.examples.length === 0 && (
                        <li className="text-gray-500 dark:text-slate-400">
                          Nothing yet — name a list and these appear. An app with
                          no lists has nothing to be told.
                        </li>
                      )}
                  </ul>
                </Well>

                <Well title="Words the router will match">
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {preview.keywords.map((word) => (
                      <Chip key={word}>{word}</Chip>
                    ))}
                  </div>
                  <Hint>
                    Filler is dropped, so &ldquo;a bot for my small family&rdquo;
                    teaches it nothing it would answer for wrongly.
                  </Hint>
                </Well>

                <Well title="Two files, both yours to edit">
                  <p className="mt-2 font-mono text-[11px] break-all text-gray-600 dark:text-slate-400">
                    {preview.app_path}
                    <br />
                    {preview.pack_path}
                  </p>
                  <Hint>
                    Outside the install, so an update never touches them and your
                    data is not sitting in a source tree.
                  </Hint>
                </Well>
              </>
            )}
          </div>
        </CardBox>
      </div>

      <div className="mt-6 mb-2 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-[#0C2F44] dark:text-white">
          Make it
        </h2>
        <span className="font-mono text-[11px] text-gray-400">
          POST /persona/scaffold
        </span>
      </div>
      <CardBox>
        <div className="flex flex-wrap items-center gap-3 p-5">
          <Button
            label={creating ? "Writing…" : "Create it"}
            color="info"
            disabled={!ready}
            onClick={create}
          />
          <span className="text-sm text-gray-600 dark:text-slate-400">
            {preview
              ? `It will appear on the apps index and the home board as ${preview.name}.`
              : "Nothing has been written yet."}
          </span>
        </div>
      </CardBox>
    </SectionMain>
  );
}
