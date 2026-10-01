import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { AutoCrfConfig, DynamicHdrMode } from "../api/types";
import { renderApp } from "../test/render";
import { QualityOptions } from "./QualityOptions";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { ...actual.api, noiseProfiles: vi.fn() } };
});

interface SourceFacts {
  hdr10: boolean;
  hdr10_plus: boolean;
  dolby_vision: boolean;
  dolby_vision_profile: number | null;
  hdr10_base_layer: boolean;
}
const sdrSource: SourceFacts = { hdr10: false, hdr10_plus: false, dolby_vision: false, dolby_vision_profile: null, hdr10_base_layer: false };
const hdrSource: SourceFacts = { hdr10: true, hdr10_plus: true, dolby_vision: false, dolby_vision_profile: null, hdr10_base_layer: true };

function Harness(props: {
  encoder?: "x264" | "x265";
  source?: SourceFacts;
  temporalFilter?: string;
  initialHdr?: DynamicHdrMode;
  onNoise?: (settings: Record<string, unknown>) => void;
}) {
  const [autoCrf, setAutoCrf] = useState<AutoCrfConfig | null>(null);
  const [hdr, setHdr] = useState<DynamicHdrMode>(props.initialHdr ?? "discard");
  return (
    <>
      <QualityOptions
        encoder={props.encoder ?? "x265"}
        contentType="FILM"
        sourceVideo={props.source ?? hdrSource}
        temporalFilter={props.temporalFilter ?? "progressive"}
        autoCrf={autoCrf}
        onAutoCrf={setAutoCrf}
        dynamicHdr={hdr}
        onDynamicHdr={setHdr}
        onApplyNoiseProfile={props.onNoise ?? vi.fn()}
      />
      <output data-testid="state">{JSON.stringify({ autoCrf, hdr })}</output>
    </>
  );
}

const state = () => JSON.parse(screen.getByTestId("state").textContent ?? "{}") as { autoCrf: AutoCrfConfig | null; hdr: string };

describe("QualityOptions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.noiseProfiles).mockResolvedValue({
      encoder: "x265",
      requires_operator_confirmation: true,
      profiles: [
        { id: "off", label: "Nincs", description: "Nincs külön kezelés.", settings: { noise_reduction: 0, tune: "none" } },
        { id: "light_denoise", label: "Enyhe zajszűrés", description: "Enyhe nr 100.", settings: { noise_reduction: 100, tune: "none" } },
      ],
    });
  });

  it("applies a noise profile's concrete settings and explains it", async () => {
    const user = userEvent.setup();
    const onNoise = vi.fn();
    renderApp(<Harness onNoise={onNoise} />);

    const select = await screen.findByLabelText("Zaj- és szemcseprofil");
    await waitFor(() => expect(select).toBeEnabled());
    await user.selectOptions(select, "light_denoise");

    expect(onNoise).toHaveBeenCalledWith({ noise_reduction: 100, tune: "none" });
    expect(screen.getByText("Enyhe nr 100.")).toBeInTheDocument();
    expect(api.noiseProfiles).toHaveBeenCalledWith("x265", "FILM");
    expect(screen.getByText(/a kódolóban történik \(nem előszűrő\)/)).toBeInTheDocument();
  });

  it("enables the automatic CRF search with defaults and lets the target and range change", async () => {
    const user = userEvent.setup();
    renderApp(<Harness />);

    expect(screen.queryByLabelText("Cél VMAF")).not.toBeInTheDocument();
    await user.click(await screen.findByRole("checkbox"));
    expect(state().autoCrf).toEqual({ enabled: true, target_vmaf: 95 });
    expect(screen.getByText(/csak a keresés kiindulópontja/)).toBeInTheDocument();

    const target = screen.getByLabelText("Cél VMAF");
    await user.clear(target);
    await user.type(target, "93.5");
    await user.clear(screen.getByLabelText("Legnagyobb CRF"));
    await user.type(screen.getByLabelText("Legnagyobb CRF"), "24");
    expect(state().autoCrf).toEqual({ enabled: true, target_vmaf: 93.5, max_crf: 24 });

    await user.click(screen.getByRole("checkbox"));
    expect(state().autoCrf).toBeNull();
    expect(screen.queryByLabelText("Cél VMAF")).not.toBeInTheDocument();
  });

  it("offers dynamic HDR only for an HDR10 x265 source and disables what the source lacks", async () => {
    const user = userEvent.setup();
    renderApp(<Harness />);

    const select = await screen.findByLabelText("Dinamikus HDR");
    expect(screen.getByText("HDR10+")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /HDR10\+ megtartása/ })).toBeEnabled();
    expect(screen.getByRole("option", { name: /Dolby Vision \(8\.1\)/ })).toBeDisabled();
    await user.selectOptions(select, "hdr10plus");
    expect(state().hdr).toBe("hdr10plus");
    await user.selectOptions(select, "auto");
    expect(state().hdr).toBe("auto");
  });

  it("hides the HDR section for SDR/x264 and warns about non-progressive timelines", async () => {
    const view = renderApp(<Harness encoder="x264" source={sdrSource} />);
    await screen.findByLabelText("Zaj- és szemcseprofil");
    expect(screen.queryByLabelText("Dinamikus HDR")).not.toBeInTheDocument();
    view.unmount();

    renderApp(<Harness initialHdr="hdr10plus" temporalFilter="ivtc_tff" />);
    expect(await screen.findByText(/IVTC\/deinterlace mellett nem vihető át/)).toBeInTheDocument();
  });

  it("marks Dolby Vision as experimental and shows the source profile", async () => {
    renderApp(
      <Harness
        source={{ ...hdrSource, hdr10_plus: false, dolby_vision: true, dolby_vision_profile: 7 }}
        initialHdr="dolby_vision"
      />,
    );
    expect(await screen.findByText("Dolby Vision P7")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /HDR10\+ megtartása/ })).toBeDisabled();
    expect(screen.getByText(/Kísérleti: ha a kész MKV-ban hiányzik/)).toBeInTheDocument();
  });

  it("tolerates a failing noise-profile endpoint", async () => {
    vi.mocked(api.noiseProfiles).mockRejectedValue(new Error("nem elérhető"));
    renderApp(<Harness />);
    expect(await screen.findByText("A zajprofilok nem tölthetők be.")).toBeInTheDocument();
    expect(screen.getByLabelText("Zaj- és szemcseprofil")).toBeDisabled();
  });
});
