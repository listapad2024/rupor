# Changelog

All notable changes to Rupor are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.6.0] — 2026-11-08

### Added

- **Switch the layout of selected text.** Typed a password or a phrase in the wrong keyboard
  layout? Select it and press **Shift+Pause**: the text is retyped in place as if it had
  been typed in the other layout (US English ↔ Russian ЙЦУКЕН), "ghbdtn" ↔ "привет". The
  direction is detected for each word; pressing again brings the original back. It is purely
  mechanical — no AI, nothing goes to history or *Paste last*. The shortcut can be changed or
  turned off in Settings → Dictation.
- **Several AI keys, in priority order.** Settings → AI now holds a list of keys: OpenRouter and
  Anthropic mixed, including several keys for the same provider. Drag a key (or use the arrows)
  to change the order. Cleanup and command mode use the first key; if it fails (out of credit,
  rejected, rate limit, provider outage, no network), Rupor moves straight on to the next one,
  and the failed key waits at the end of the queue for a while. Each key can be turned off,
  renamed, replaced or deleted.
- **Balance for each key.** OpenRouter keys show the balance from OpenRouter. Anthropic doesn't
  report a balance through its API, so you can enter the balance from the Console and Rupor shows
  an estimate: that amount minus what Rupor has spent through the key since. The AI spending card
  shows the balance of the key that is first in line.
- **The last 30 voice recordings are kept on disk** (`%APPDATA%\Rupor\recordings`, WAV,
  up to 300 MB in total; both limits are in Settings → History, 0 keeps none). Each dictation
  is saved before recognition, so a dictation whose text was lost can be transcribed again
  under the new **History → Recordings** tab. The result goes to History and to *Insert last*.
  A long recording that produced no text now says it was saved and links to it.
- **Play a recording.** History → Recordings has *Play / Stop* on every recording, and each
  dictation in History that still has its recording gets a play button.

### Changed

- Existing OpenRouter and Anthropic keys move into the new list automatically, with the provider
  you had selected first.
- Spending for Claude Haiku 5.5, Sonnet 5.5 and Opus 5.5 is now counted at their own prices
  (Opus 5.5 was priced as Opus 5 before, Haiku 5.5 was not counted at all).

### Fixed

- Claude Haiku 5.5 works for cleanup: Rupor no longer sends it `temperature`, which the model
  rejects (before, every call failed and went to the next key).
- **The AI no longer adds its own notes to the text.** A model could append "Note: …" explaining
  why it did not apply a rule. The prompt now forbids it, and such a trailing note that you did
  not dictate is removed.
- The clipboard is restored when pasting fails (focus lost, locked screen, UAC), instead of
  keeping the dictation in it.
- Shortcuts no longer stop working silently: if the keyboard hook can't be installed, Rupor
  retries and then says so.
- A settings file held by an antivirus or OneDrive no longer stops the engine or resets the
  settings; a broken settings file is kept next to it (`settings.broken-<time>.json`), and the AI
  keys stay in the list.
- A microphone that disappears while recording no longer leaves the audio stream open; a sound
  that can't be played no longer breaks a recording.
- Dictionary terms with their own capitalisation ("iPhone", "macOS") are no longer turned into
  "IPhone" at the start of a sentence.
- The mouse button used for dictation works again after Esc; "Miro" is no longer heard as «мир».
- Searching History with a long query (a pasted paragraph) no longer fails, and a TLS drop
  during the CUDA download is retried like any other dropped connection.
- **Long dictations no longer vanish as "garbage".** The loop detector measured how well the
  whole text compressed; ordinary Russian text longer than about 2,000 characters
  (2.5 minutes of speech) compresses past Whisper's threshold with no repetition at all, so a
  153-second dictation was thrown away as "No speech detected". Compression is now checked in
  400-character windows, like Whisper's own 30-second segments. And when both passes still look
  like garbage only because of a stray foreign glyph, the text is cleaned and inserted instead
  of dropped.

## [0.4.3] — 2026-10-18

### Fixed

- **The setup wizard no longer swaps the model you are downloading.** Clicking *Download*
  before the suggestion for your computer arrived (a few seconds on a computer without an NVIDIA
  card) let the suggestion replace the model afterwards: the first model was downloaded for
  nothing and the step asked to download again. *Download* now keeps the model shown, and a
  wizard started again from Settings never replaces the model chosen there.
- **Model folder: progress and errors stay in step with what is running.** After a failed move,
  a later model download showed the old move error instead of its own progress; a double-click
  on *Move* reported a false "the model is being moved" error and lost track of the real move;
  a failed download is now shown instead of disappearing; in the wizard, the result of an
  earlier move no longer appears on the model row after another folder is picked.
- **"Default" works when the model folder was set to the default path by hand.** It now
  switches the setting back to the default (following `HF_HOME` / `HF_HUB_CACHE`) instead of
  doing nothing.
- **Moving the model can no longer delete its only copy.** A new folder that is the current one
  under another path (a junction or symbolic link, a `subst` drive, a mapped drive and its UNC
  path, another letter case) is recognised: only the setting is saved, nothing is moved or
  deleted. A folder inside the model's own folder or in `.locks` is refused. The old copy is
  deleted only after the model has really loaded from the new folder, and never when the two
  copies are the same files.
- **An incomplete copy in the new folder is completed instead of trusted.** If the new folder
  already held the model without some of its files (for example `preprocessor_config.json`,
  left out by an older tool), the move now copies the missing files rather than deleting the
  complete old copy.
- **A model chosen while another one is being moved gets downloaded.** It used to stay "not
  downloaded" until Rupor restarted; the download now starts when the move ends (into the new
  folder, or into the old one if the move failed).
- **Better suggestions for older NVIDIA cards and 8 GB laptops.** The wizard only suggests a
  precision the card supports: `int8` on GTX 10xx cards, which cannot run `float16`, and the
  processor on older cards. A card that CUDA cannot see (the driver is too old) no longer
  counts. RAM is the installed amount, so an 8 GB laptop with integrated graphics counts as
  8 GB and gets `small`.
- **The same model shows the same size everywhere.** Download and move progress and the
  free-space messages now use the same megabytes (10⁶ bytes) as the model list: `small` is
  "486 MB" in both places, not 486 in the list and 464 in the progress line.
- **An unreachable network folder no longer freezes the window.** Checking the new model folder
  (about 20 s for an offline NAS) no longer holds up the other commands from the window.

## [0.4.2] — 2026-10-18

### Added

- **Choose where the speech model is stored.** The model no longer has to live in the shared
  Hugging Face cache on the system drive: pick any folder (for example
  `D:\Models\whisper-models`) in the setup wizard or in Settings → Dictation → Recognition →
  *Model folder* (`model_dir`; empty — the Hugging Face cache as before, so existing installs
  keep working). The folder uses the Hugging Face cache layout, the same as faster-whisper's
  `download_root`, so other faster-whisper programs can share it. A model that is already
  downloaded can be moved there: instantly on the same drive, or copied with a progress bar to
  another drive (relative symlinks are kept; without the right to create them the files are
  written in place, without a second copy), with a free-space check, into a temporary folder
  that is renamed only when the copy is complete; the old copy is deleted afterwards. Dictation
  keeps working on the loaded model during the move; if the move fails, nothing changes. Without
  moving, the model is downloaded into the new folder unless it is already there. Core: new
  command `model_dir_set`, new `setup_status` fields `model_dir` and `model_dir_default`.
- **The setup wizard suggests a model for your computer.** It looks at the NVIDIA graphics card
  and its memory, RAM and processor threads (`model_recommend`): `large-v3-turbo` on a card
  with 4 GB or more, the same model in a memory-saving mode (int8) on 2–4 GB, `small` on smaller
  cards or a strong processor, `base` on modest computers — with a reason in plain words
  ("RTX 3060 Ti graphics card with 8 GB — the best model for Russian and English runs
  fast"). The other models (tiny … large-v3) are listed with their exact size from Hugging Face
  (a built-in table if there is no network), accuracy and speed; the suggestion is picked for
  you on a fresh install, and a model that is already downloaded is never swapped silently.
  Settings → Dictation → *Model* now also offers `base` and `tiny`.
- **Pick the folder with the system dialog.** *Browse…* next to the model folder opens the
  Windows folder picker (Tauri dialog plugin, allowed for the main window only); a path can also
  be pasted, including one copied with Explorer's *Copy as path* (the quotes are dropped).

### Fixed

- **Setup wizard cards without doubled padding.** The cards on the *GPU and model* and
  *Shortcuts* steps had extra space inside around their rows.

## [0.4.1] — 2026-10-14

### Added

- **The indicator no longer disappears.** It is put back on top every time another window is
  activated and when recording starts (a full-screen RDP window used to cover it), returns to
  the primary monitor if a display change leaves it off-screen, reloads itself if its WebView2
  page crashes or keeps hanging (the whole shell restarts if the browser process dies; GPU
  process restarts are left to WebView2), and re-renders after an error instead of staying an
  empty transparent window. Errors are written to `ui.log`. New tray item *Bring back the
  indicator* does all of that by hand and remembers the default position.
- **Similar spellings: one dictionary term is enough.** Spellings that sound like a term are
  fixed without listing variants — Latin or Cyrillic, joined or split, with Russian case endings:
  "Zenobowser", «Xena браузер», «в зенно браузере» → ZennoBrowser. On by default; switch:
  Dictionary → *Recognize similar spellings* (`dict_fuzzy`). Real words are never replaced:
  the Windows spell checker (Russian and English) tells a misrecognition («кубернетис»,
  "Xenoposter") from a word that merely sounds like a term («курсор», «редис», "cloud"). Terms
  made of two ordinary words and very short terms still need a variant; words you often
  dictate yourself count as real words; without the Windows spelling dictionaries only exact
  variants work; paths, addresses and file names are left alone. Words it would replace are
  also kept out of the recognition hint and the AI's term list. The build self-check now
  covers the spell checker.
- **See how the dictionary fills up.** Dictionary → *Recently added* lists the words learned
  from your corrections: what was added, when, and whether Rupor added it or you confirmed it,
  with an Undo button on each row (`dict_learn_journal`). In *Add automatically* mode every
  correction that passes the filters is now added right away with an Undo notification, which
  stays for 12 seconds; doubtful ones used to be only suggested, and the question was easy to
  miss. *Ask first* still asks.

### Fixed

- **AI cleanup works with "thinking" models.** Requests to OpenRouter now turn reasoning off,
  so models such as DeepSeek V4.1 Flash no longer spend the answer budget on thinking and
  return nothing. On 20 Russian test dictations DeepSeek V4.1 Flash was more accurate than
  Claude Haiku 4.5 (WER 2.4 % vs 4.0 %), about twice as fast and ~15× cheaper.
- **No more garbage from recognition.** When Whisper is unsure it falls back to guessing and
  could produce CJK characters, random letter sets ("jpuy, jpac") or subtitle phrases
  («ДИНАМИЧНАЯ МУЗЫКА»), which were inserted as is and even became "frequent words" that
  poisoned the next hints. Such output is now recognised again without the hint and without
  guessing; if it is still garbage, nothing is inserted (*No speech detected*).

- Shortcuts for *Paste last* / *Copy last* that include Alt or Win (for example Win+Alt+V) no
  longer open the window menu or the Start menu when the keys are released.

- **The dictionary has the last word.** AI cleanup could put back a misrecognition that is in
  the dictionary as a variant ("Xenoposter" instead of "Zennoposter"). The AI's reply now goes
  through the dictionary again: in both AI modes and in the *Check cleanup* button.
- A misrecognition that reached the history before it was added to the dictionary no longer
  reinforces itself. Words the dictionary would replace are left out of the recognition hint
  and of the AI's term list, and the text before the cursor used as a hint is corrected by the
  dictionary first.

## [0.4.0] — 2026-09-27

### Added

- **English interface.** Every screen, the setup wizard, the indicator, the tray menu and all
  messages from the engine (notifications, errors, settings validation) are available in
  English. In English the app is called **Rupor**.
- **Interface language switch**: Settings → General → Language — *System* (default), *Русский*,
  *English*. *System* follows the Windows display language (Russian for ru/be/uk/kk, English
  otherwise). The language changes on the fly, without a restart; the setup wizard now starts
  with this choice.
- **Dictation language** setting: *Auto* (default), *Русский*, *English*. In *Auto* each phrase
  is recognized in the interface language unless the model is confident it's the other one
  (`en_threshold` is now the threshold for the second language). A fixed language skips
  detection.
- **English AI cleanup.** English dictation is cleaned up by an English prompt with the same
  rules: filler words (*um, uh, like, you know, basically*), self-corrections (*no wait*,
  *scratch that*), punctuation, numbers, terms and lists; questions in the text are never
  answered. Styles have English instructions. Command mode uses the interface language.
  Benchmark on 12 English cases: 1.1% WER against 25% for the raw text, no fillers left
  ([docs/BENCH.md](docs/BENCH.md)).
- Dates, times, numbers, currency and plurals are formatted for the selected language.
- The installer asks for its language (English or Russian); the setup wizard, the uninstaller
  and the Installed apps entry use the name in that language.
- `?lang=en|ru` for the interface demo mode; English demo data.

### Changed

- The dictionary learns more carefully from English text: capitalizing a common English word
  (Monday, January) is not learned, plain names are only suggested; rare English words are kept
  as weak hints. Word frequencies are rebuilt from history once after the update.
- Setting names in error messages match the names in Settings.

### Notes

- To keep the install path and autostart of existing installations, the install folder,
  shortcuts and the name built into `rupor.exe` stay "Рупор".

## [0.3.0] — 2026-09-27

First public release.

- Voice dictation into any window: hold Ctrl+Win and speak, or double-press for hands-free mode;
  recording can also be started with the mouse.
- Local speech recognition on the GPU (faster-whisper `large-v3-turbo`, CUDA) with a CPU
  fallback; no internet needed after the first-run download.
- Text is pasted via the clipboard or typed as keystrokes; keystrokes work in RDP sessions,
  RemoteApp, Citrix and virtual machine consoles even with the clipboard disabled.
- Broadcast to RDP: one phrase goes to several selected remote sessions.
- Optional AI cleanup through your own OpenRouter, Anthropic or Ollama account, with styles
  (Standard, Business, Chat message, Code / prompt); the raw text is inserted if the AI fails.
- Command mode: select text and tell the AI what to do with it.
- Dictionary of terms and spelling variants that learns from your corrections, frequent words
  and window context as recognition hints, snippets.
- App and window title rules for the insert method and AI style.
- History with search and a draft tab, dictation stats, lowering other apps' audio while
  recording.
- Tray icon, start with Windows, light and dark themes, first-run wizard; a per-user NSIS
  installer without administrator rights.

[0.6.0]: https://github.com/listapad2024/rupor/compare/v0.4.3...v0.6.0
[0.4.3]: https://github.com/listapad2024/rupor/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/listapad2024/rupor/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/listapad2024/rupor/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/listapad2024/rupor/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/listapad2024/rupor/releases/tag/v0.3.0
