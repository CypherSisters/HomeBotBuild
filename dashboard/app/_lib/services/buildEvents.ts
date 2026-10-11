/**
 * "The build changed" — one notification, for the views that list its apps.
 *
 * Installing an app on this box is a rescan, and the box does it before the
 * scaffold call resolves. But the browser has already painted: the apps index
 * and the home board's Apps tile are both holding the list they fetched on
 * mount, and neither remounts just because a different route wrote a file.
 *
 * So the writer says so once and the readers refetch. This is deliberately NOT
 * a store of app state — a cache here would be a third place where "which apps
 * exist" is recorded, and the whole point of /apps/installed and /persona/list
 * is that the box is the only one.
 *
 * Copyright (c) Cypher Sisters. Apache-2.0 License.
 */

type Listener = () => void;

const listeners = new Set<Listener>();

/** Subscribe; returns the unsubscribe, shaped for a useEffect cleanup. */
export function onBuildChanged(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Tell every mounted list that the set of installed apps is not what it was. */
export function notifyBuildChanged(): void {
  for (const listener of Array.from(listeners)) {
    try {
      listener();
    } catch {
      /* one bad subscriber is not the others' problem */
    }
  }
}
