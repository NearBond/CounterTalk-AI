import type { Player } from "../lib/hooks";
import { RetryAlert } from "./RetryAlert";

type Props = Omit<Player, "playAt" | "playFrom" | "src"> & { src: string };

/** A page's one player and, when its recording can't be loaded, the message saying so, with Try
 * again: the visit page's (batch 14) and an issue's (batch 15). */
export function PagePlayer({ audio, onError, onLoadedMetadata, unplayable, deleted, retry, tryAgain, src }: Props) {
  return (
    <>
      {/* Seekable: the server answers Range requests (batch 6). */}
      <audio
        ref={audio}
        controls
        preload="metadata"
        src={src}
        onError={onError}
        onLoadedMetadata={onLoadedMetadata}
        className="w-full"
      >
        <track kind="captions" />
      </audio>
      {unplayable && deleted && (
        <p role="status" className="text-gray-700">
          Audio deleted under the retention policy. Its transcript and quotes are kept.
        </p>
      )}
      {unplayable && !deleted && (
        <RetryAlert retryRef={retry} onRetry={tryAgain}>
          The recording can't be played right now. Everything else on this page still works.
        </RetryAlert>
      )}
    </>
  );
}
