import { render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { recordingDeleted } from "../lib/api";
import { jsonResponse as json } from "../testUtils";
import { PagePlayer } from "./PagePlayer";

function player(unplayable: boolean, deleted: boolean) {
  render(
    <PagePlayer
      audio={createRef<HTMLAudioElement>()}
      onError={() => {}}
      onLoadedMetadata={() => {}}
      unplayable={unplayable}
      deleted={deleted}
      retry={createRef<HTMLButtonElement>()}
      tryAgain={() => {}}
      src="/api/visits/v1/audio"
    />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PagePlayer", () => {
  it("says a recording deleted under the retention policy is gone, with no Try again", () => {
    player(true, true);

    expect(screen.getByRole("status")).toHaveTextContent(
      "Audio deleted under the retention policy. Its transcript and quotes are kept.",
    );
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("offers Try again for a recording that's only not loading", () => {
    player(true, false);

    expect(screen.getByRole("alert")).toHaveTextContent("The recording can't be played right now.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("recordingDeleted", () => {
  it.each([
    [404, { error: { code: "audio_deleted", message: "Audio deleted under the retention policy" } }, true],
    [404, { error: { code: "audio_not_found", message: "The recording isn't ready to play yet" } }, false],
    [500, { error: { code: "unexpected_error", message: "Something went wrong." } }, false],
  ])("for a %i %j is %s", async (status, body, gone) => {
    const fetched = vi.fn(async () => json(status, body));
    vi.stubGlobal("fetch", fetched);

    expect(await recordingDeleted("/api/visits/v1/audio")).toBe(gone);
    expect(fetched).toHaveBeenCalledWith("/api/visits/v1/audio", { headers: { Range: "bytes=0-0" } });
  });

  it("isn't known to be deleted when no answer came", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("offline"))));

    expect(await recordingDeleted("/api/visits/v1/audio")).toBe(false);
  });
});
