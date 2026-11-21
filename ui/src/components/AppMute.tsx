// «Настройки → Диктовка → Голосовые чаты во время диктовки» (app_mute_*).
// Пока держите клавишу диктовки, ядро выключает микрофон (сессию захвата Core Audio) и,
// по желанию, звук приложений из списка — live chat Codex, ChatGPT — и возвращает после.
import { Info } from "lucide-react";
import { t } from "../i18n";
import type { Settings, SettingsPatch } from "../lib/types";
import { ChipInput, Switch } from "./ui/controls";
import { Card, SectionTitle, SettingRow } from "./ui/display";

/** Значения ядра по умолчанию — на случай, если старое ядро их ещё не присылает. */
export const APP_MUTE_DEFAULT_APPS = ["codex.exe", "chatgpt.exe"];

export function AppMuteSection({
  s,
  set,
}: {
  s: Partial<Pick<Settings, "app_mute_enabled" | "app_mute_apps" | "app_mute_output">>;
  set: (p: SettingsPatch) => Promise<boolean>;
}) {
  const enabled = s.app_mute_enabled ?? true;
  const output = s.app_mute_output ?? true;
  const apps = s.app_mute_apps ?? APP_MUTE_DEFAULT_APPS;

  return (
    <section>
      <SectionTitle hint={t("settings.dictation.appMute.hint")}>{t("settings.dictation.appMute.title")}</SectionTitle>
      <Card padded={false}>
        <SettingRow title={t("settings.dictation.appMute.enabled")} description={t("settings.dictation.appMute.enabledHint")}>
          <Switch
            checked={enabled}
            label={t("settings.dictation.appMute.enabled")}
            onChange={(v) => void set({ app_mute_enabled: v })}
          />
        </SettingRow>
        <SettingRow title={t("settings.dictation.appMute.output")} description={t("settings.dictation.appMute.outputHint")}>
          <Switch
            checked={output}
            disabled={!enabled}
            label={t("settings.dictation.appMute.output")}
            onChange={(v) => void set({ app_mute_output: v })}
          />
        </SettingRow>
        <SettingRow stacked title={t("settings.dictation.appMute.apps")} description={t("settings.dictation.appMute.appsHint")}>
          <ChipInput
            mono
            values={apps}
            onChange={(v) => void set({ app_mute_apps: v })}
            placeholder={t("settings.dictation.appMute.placeholder")}
          />
        </SettingRow>
      </Card>
      <p className="mt-2.5 flex gap-2 px-1 text-[12px] leading-relaxed text-subtle">
        <Info className="mt-[3px] size-3.5 shrink-0" />
        <span>{t("settings.dictation.appMute.note")}</span>
      </p>
    </section>
  );
}
