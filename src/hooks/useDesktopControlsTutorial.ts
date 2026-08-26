import { useCallback, useEffect, useRef, useState } from "react";

const STORAGE_KEY = "tooncar.desktop-controls-tutorial";
const TUTORIAL_VERSION = 1;
const ONE_DAY_MS = 24 * 60 * 60 * 1000;

type TutorialProgress = {
  version: number;
  completedAt: number;
  suppressUntil: number;
};

function readTutorialProgress(): TutorialProgress | null {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null");
    if (value === null || typeof value !== "object") return null;

    const progress = value as Partial<TutorialProgress>;
    if (
      typeof progress.version !== "number" ||
      typeof progress.completedAt !== "number" ||
      typeof progress.suppressUntil !== "number"
    ) {
      return null;
    }

    return progress as TutorialProgress;
  } catch {
    return null;
  }
}

export function useDesktopControlsTutorial(enabled: boolean) {
  const [visible, setVisible] = useState(false);
  const [fadingOut, setFadingOut] = useState(false);
  const revealTimeoutRef = useRef<number | null>(null);
  const dismissTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    if (enabled === false) {
      if (revealTimeoutRef.current !== null) window.clearTimeout(revealTimeoutRef.current);
      setVisible(false);
      return;
    }

    const desktopPointer = window.matchMedia("(any-pointer: fine)");
    const updateVisibility = () => {
      if (revealTimeoutRef.current !== null) {
        window.clearTimeout(revealTimeoutRef.current);
        revealTimeoutRef.current = null;
      }
      if (desktopPointer.matches === false) {
        setVisible(false);
        return;
      }

      const progress = readTutorialProgress();
      const isSuppressed = progress !== null && Date.now() < progress.suppressUntil;
      const isCompleted = progress?.version === TUTORIAL_VERSION;
      if (isSuppressed === true || isCompleted === true) {
        setVisible(false);
        return;
      }

      revealTimeoutRef.current = window.setTimeout(() => {
        setFadingOut(false);
        setVisible(true);
        revealTimeoutRef.current = null;
      }, 1000);
    };

    updateVisibility();
    desktopPointer.addEventListener("change", updateVisibility);
    return () => {
      desktopPointer.removeEventListener("change", updateVisibility);
      if (revealTimeoutRef.current !== null) window.clearTimeout(revealTimeoutRef.current);
    };
  }, [enabled]);

  useEffect(
    () => () => {
      if (dismissTimeoutRef.current !== null) window.clearTimeout(dismissTimeoutRef.current);
    },
    [],
  );

  const complete = useCallback(() => {
    if (dismissTimeoutRef.current !== null) return;
    if (revealTimeoutRef.current !== null) window.clearTimeout(revealTimeoutRef.current);
    setFadingOut(true);
    dismissTimeoutRef.current = window.setTimeout(() => {
      const completedAt = Date.now();
      const progress: TutorialProgress = {
        version: TUTORIAL_VERSION,
        completedAt,
        suppressUntil: completedAt + ONE_DAY_MS,
      };

      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(progress));
      } catch {
        // The tutorial should remain dismissible when storage is unavailable.
      }
      setVisible(false);
      setFadingOut(false);
      dismissTimeoutRef.current = null;
    }, 250);
  }, []);

  const show = useCallback(() => {
    if (revealTimeoutRef.current !== null) window.clearTimeout(revealTimeoutRef.current);
    if (dismissTimeoutRef.current !== null) window.clearTimeout(dismissTimeoutRef.current);
    revealTimeoutRef.current = null;
    dismissTimeoutRef.current = null;
    setFadingOut(false);
    setVisible(true);
  }, []);

  return { visible, fadingOut, complete, show };
}
