import { useCallback, useEffect, useState } from "react";

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

  useEffect(() => {
    if (enabled === false) {
      setVisible(false);
      return;
    }

    const desktopPointer = window.matchMedia("(any-pointer: fine)");
    const updateVisibility = () => {
      if (desktopPointer.matches === false) {
        setVisible(false);
        return;
      }

      const progress = readTutorialProgress();
      const isSuppressed = progress !== null && Date.now() < progress.suppressUntil;
      const isCompleted = progress?.version === TUTORIAL_VERSION;
      setVisible(isSuppressed === false && isCompleted === false);
    };

    updateVisibility();
    desktopPointer.addEventListener("change", updateVisibility);
    return () => desktopPointer.removeEventListener("change", updateVisibility);
  }, [enabled]);

  const complete = useCallback(() => {
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
  }, []);

  return { visible, complete };
}
