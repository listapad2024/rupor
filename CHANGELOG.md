# Changelog

All notable changes to Rupor are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.8.3] — 2026-11-22

### Added

- **Experimental own-voice score (off by default).** With **Settings → History → Own-voice
  score** turned on, Rupor compares the voice of each dictation with your voice profile and
  writes the score to a journal on this computer (`%APPDATA%\Rupor\speaker\journal.jsonl`).
  It only measures: dictation works as before and nothing is blocked. The comparison runs on
  the processor after the text is inserted, so insertion isn't delayed. The profile is built
  from your own voice recordings with `rupor-core.exe --speaker-enroll`; the speaker model
  and the profile stay in your data folder and are not shipped with Rupor.

### Changed

- **Rupor can keep thousands of voice recordings.** The limit goes up from 500 to 10,000
  recordings, and **Settings → History** now offers up to 10,000 recordings and 20 GB of
  space. The default stays at the last 30. Saving a recording no longer re-reads every
  saved one, so dictation is as fast with thousands of recordings as with thirty.

### Fixed

- **Dictation wasn't inserted while a voice chat was open.** When Rupor pressed the chat's
  **Mute / Unmute microphone** button, the Codex / ChatGPT window came to the front, so the
  text was held back with “The window changed”. Rupor now puts the window you were
  dictating into back in front.
- **The voice chat's microphone flickered on and off.** A quick tap of the dictation key
  followed by a dictation could press the chat's button several times in a row, briefly
  unmuting the chat mid-recording. Now only the latest state is applied, and a tap released
  before the chat window is read doesn't press anything.
- **AI cleanup sometimes inserted its own working.** Now and then the model returned the
  dictation twice — as it was and corrected — with a comment in between (“Fixed: … Result:
  …”), and all of it went into the window. Such a reply is now rejected and the recognized
  text is inserted as is, with the usual notice.
- **Dictation hung for minutes when other programs filled the video memory.** With the
  graphics card's memory taken by other programs, Windows pushed the speech model out to
  ordinary memory, and a 20-second dictation took up to 10 minutes to recognize. Now a
  dictation the GPU doesn't finish in time (4 s plus 0.3 s per second of audio) is
  recognized again on the processor, which takes a few seconds, with a notice. Until the
  late GPU pass ends, the next dictations go straight to the processor. The log records how
  much video memory Rupor had against what Windows allowed it.

## [0.8.2] — 2026-11-21

### Fixed

- **The voice chat didn't come back after dictation.** 0.8.1 paused the chat's audio
  process, and after a few seconds without microphone data Codex decided the microphone had
  failed and dropped it. Now Rupor uses the chat's own button instead: while you hold the
  key it presses **Mute microphone** in the Codex / ChatGPT window and **Unmute microphone**
  when you release it, so the conversation carries on. A chat you muted yourself is left
  alone, and if Rupor closes mid-recording, the chat's microphone is turned back on the next
  time it starts.

## [0.8.1] — 2026-11-21

### Fixed

- **Dictation recorded silence while a voice chat was open.** 0.8.0 muted the voice chat's
  own microphone session, but on many microphones Windows applies that mute to every app
  recording from the device, Rupor included. Now Rupor pauses the voice chat's audio process
  for the time you hold the key instead: the chat doesn't hear you and stays silent, the
  window and the conversation stay open, and Rupor hears the microphone as usual. The
  Codex desktop app is `ChatGPT.exe` on Windows, which is already on the default list. The
  "mute their audio too" switch is gone: a paused chat makes no sound anyway.

## [0.8.0] — 2026-11-21

### Added

- **Voice chats don't hear your dictation.** While you hold the dictation key, Codex live chat,
  ChatGPT voice and other voice chats on the list can't hear you: Rupor mutes their microphone
  (their own capture session in Windows, the rest of the system and Rupor itself hear the mic
  as usual) and, optionally, their audio, so the assistant's reply doesn't play over you. Your
  voice goes only into the dictated prompt; release the key and the chat hears and talks
  again. It kicks in the moment the key goes down, so the first words don't leak into the chat.
  Settings → Dictation → *Voice chats while dictating*: on/off, "mute their audio too", and the
  list of apps (exe names; `codex.exe`, `chatgpt.exe` by default). If Rupor closes
  mid-recording, the chat's microphone and audio are restored the next time it starts.

### Added

- **Live meeting transcription and translation.** Rupor recognises the captured meeting audio
  with the model already loaded for dictation and translates foreign speech into the language
  you choose (Russian by default) through your AI provider. Speech already in that language is
  only transcribed, and translation can be switched off. Phrases are cut by Silero VAD as
  people pause (long monologues are split at the quietest moment, never mid-sound), and the
  speech language is detected per phrase or can be fixed in Settings.
- **Near-simultaneous: words appear while people are still talking.** The current phrase is
  re-recognised every 0.8 s; words show up under a second after they are spoken (the not yet
  certain end of the phrase in muted text) and are fixed once two passes agree. The
  translation follows phrase by phrase in small pieces and is shown word by word as the AI
  writes it, about 2.5 seconds behind the speaker. The language is checked at the start of
  every phrase, so a phrase in another language in the middle of a meeting — Russian in an
  English call — is recognised at once and only transcribed. When a phrase ends it is
  recognised once more with the more accurate setting, and its translation is redone as a
  whole and replaced in place (*Refine the translation of each phrase*, on by
  default). Rupor keeps the connection to the AI provider open (through your system proxy) and
  asks OpenRouter for its fastest route. Subtitle hallucinations such as "Thank you for
  watching" never make it into the transcript.
- **The meeting translation window.** A narrow panel docked to the right edge of the screen,
  full height, shows the live transcript: the translation in large type, the original in
  smaller muted text under it (can be hidden), the time of each line, "translating…" while a
  translation is on its way, and a clear status — listening with a small sound meter, waiting
  for the GPU while you dictate, no audio, or the error. It scrolls along with new lines and
  stays put when you scroll back up, with a *New lines* button to jump down. In the panel you
  pick the audio source, start and stop, change the target and speech languages, switch
  translation and the original on or off, change the text size, copy the whole transcript and
  open the saved file. The panel never takes the keyboard focus, so dictation keeps typing into
  your meeting or document; it is hidden from screen sharing by default and stays above other
  windows, just under the indicator. Drag it by the header or resize it from the edges — its
  place is remembered, and *Dock to the right edge* brings it back. Open it from *Meeting
  translation* on the Home page, in the tray menu, or with *Start transcribing* on the *New
  meeting detected* card.
- **Minimize, close and a compact bar for the translation window.** The header now has
  standard *Minimize* and *Close* buttons. *Minimize* turns the panel into a small bar at the
  bottom right of the screen, always on top and never taking the focus: the latest line runs
  in it as a single-line ticker (the newest words stay visible), with *Expand*, *Stop* and
  *Hide to tray*; double-click it to get the panel back where it was, drag it anywhere — the
  place is remembered. *Close* simply hides the window when nothing is running; during a
  translation it asks first: *Minimize* or *Stop and close*. A hidden window keeps translating
  "in the tray": the tray item reads *Meeting translation (running)* and its tooltip *meeting
  translation running*; clicking it brings the panel back.
- **Translation stops by itself when the meeting ends.** When the meeting app releases the
  microphone for 15 seconds and nobody has spoken for half a minute (for *All computer sound*
  — after three minutes of silence), the window and the bar show *The meeting has ended —
  stopping in 10 s* with *Keep going*; if speech returns or the microphone is taken again, the
  countdown is dropped on its own. Then it is an ordinary *Stop*: the transcript is saved, the
  final title and the notes are written, and a notice says so. A muted microphone in Meet does
  not count as the end. Closing the meeting app stops the translation right away, also with
  notes. Turn it off in *Settings → Meetings → Stop automatically when the meeting ends*.
- **Settings → Meetings**: languages, translation on/off, refining each line's translation,
  translation model and timeout, the original, text size, hiding from screen sharing, the window
  position, the meeting suggestion (also for desktop meeting apps, the programs never to suggest
  for, window titles to name the meeting), saving transcripts and their folder, naming them by
  content, meeting notes and their model.
- **Dictation always comes first.** While you dictate, or a dictation is waiting to be
  recognised, the meeting waits for the model, so dictation is not slowed down; phrases that
  piled up meanwhile are recognised together right after. The meeting never shows up in the
  indicator, never changes your dictation history and keeps the model loaded while it runs.
- **Meeting transcripts are saved** as Markdown, one file per meeting, in
  `%APPDATA%\Rupor\meetings` or a folder you choose: the time, the translation and the original
  of every line. Each line is written to disk as soon as it is ready, so a crash loses nothing
  already written.
- **Meeting recordings get meaningful names** instead of the program's name: after eight
  phrases and a minute and a half, and again after *Stop*, the AI reads the transcript and
  titles the recording ("2026-10-19 12-22 — Billing move to ClickHouse.md"); the file and its
  heading are renamed without losing a line, and the title shows in the translation window.
  Without a key, or with *Name recordings by their content (AI)* off in *Settings →
  Meetings*, the name comes from the meeting window ("Google Meet", at most 80 characters) or
  the first words spoken. Title calls appear in *Spending* as *Meeting titles*.
- **Meeting notes: the full transcript plus notes.** When a meeting ends (five phrases or two
  minutes of speech and up), a stronger model — Claude Sonnet 5.5 by default, about $0.07 per
  hour of meeting — writes notes in the translation language: the title, *Summary*,
  *Decisions*, *Action items* as checkboxes ("who — what — when", only what was actually said),
  *Open questions* and *Key moments* with the time of the line they come from. They appear on
  the new *Notes* tab of the translation window — written live, with checkboxes, and each time
  is a link to that line of the transcript; a dot on the tab and *Notes are ready* tell you
  when they arrive — and at the top of the transcript file, above *Full transcript*. The notes'
  title becomes the recording's final title. *Make notes now* works during the meeting and
  after it, *Refresh* writes them again, *Copy notes* puts them on the clipboard. Long
  meetings are summarised in parts and then merged. In *Settings → Meetings* pick the model
  (Sonnet 5.5, Opus 5.5, Haiku 5.5, DeepSeek V4 Pro, Gemini 3.1 Pro or your own, with the price
  of an hour shown) or turn automatic notes off. Without a key or on an error the transcript
  file is left as it is, the window shows why, and you get one notice. Notes appear in
  *Spending* as *Meeting notes*; the transcript file also gets the meeting's duration.
- **AI spending counts answers that came but didn't fit**: a reply cut at the token limit, a
  "title" that isn't one, an empty translation, unusable notes are recorded as failed calls
  with their real price instead of $0 (the average price per call still uses successful calls
  only).
- **Translation problems don't interrupt the meeting.** Without an AI key, or with a rejected
  key or an empty balance, the meeting is transcribed without translation and you get one
  notice with a button to the AI settings; translation resumes once a key is set. Network
  errors only mark the affected lines; after three in a row translation pauses for 20 seconds.
- **Rupor notices a meeting by itself** in any browser (Chrome, Edge, Brave, Yandex Browser,
  Opera, Vivaldi, Firefox, Comet, Arc…) and in desktop meeting apps (Zoom, Teams, Telegram,
  Discord, MAX and others; can be turned off): when a program has held the microphone for 10
  seconds and plays sound, Rupor offers to start transcription, once per call. Calls handled by
  a helper process of the app (MAX's call service, Zoom's screen sharing) count as that app. In
  a browser tab whose title doesn't look like a meeting, the microphone has to be held for a
  minute, so a voice message or dictation in one tab while music plays in another doesn't
  trigger it, and *Not now* on such a suggestion pauses them for that browser for 30 minutes. A
  microphone held for hours (a forgotten tab, a voice assistant) is not taken for a call. The signal is the
  Windows microphone usage record, only read, so it works whichever tab has the call and costs
  about a millisecond every two seconds; nothing is polled while a meeting runs. Records left
  behind by a program that crashed with the microphone open are ignored, and so are Rupor
  itself, OBS, Dragon, ffmpeg and Python (the list is configurable, and *Don't offer for …* adds
  to it). The meeting is named from its window titles ("Google Meet", "Zoom", "Kontur.Talk"…).
- **Automatic source choice prefers the program on the microphone** when it plays sound, so a
  browser playing music no longer wins over the browser with the call, and a tab silently
  holding the microphone doesn't win over the browser that is actually playing.
- **"The meeting seems to be over"**: when the meeting program has released the microphone for
  30 seconds and its sound has stopped, the translation window says so and offers *Stop*;
  transcription never stops by itself.
- **The "New meeting detected" card.** When Rupor notices a meeting, a small card appears in
  the top-right corner of the screen, above other windows and without taking the focus from
  your call: the program and the meeting ("Brave — Google Meet"), whether it will translate
  (and into which language) or only transcribe, *Start transcribing*, *Not now* and *Don't
  suggest for {program}*. *Start transcribing* turns the card into the translation panel at its
  usual place and starts right away. Unanswered, the card hides itself after 45 seconds (a thin
  bar shows the time left). If the translation panel is already open, the suggestion appears in
  it as a strip under the header.
- **Live lines in the translation window.** Words appear while people are still speaking, the
  part recognition may still change in muted italics; the translation grows word by word with a
  thin cursor, and three dots hold its place until the first words arrive, so the line doesn't
  jump. Words corrected at the end of a phrase (the final recognition pass, the translation of
  the whole phrase) fade in place, and lines updated above don't shift what you are reading. The
  panel follows the text smoothly instead of jumping at every word, a dot marks the phrase still
  being spoken, a phrase that turns out to be noise disappears, ✓✓ marks a refined translation, and the footer shows the average delay — text
  and first words of the translation after the end of a phrase.
- **Translation is a separate line in AI spending** (`translate`), with its own model and
  timeout settings (`translate_model`, `translate_timeout_s`).
- **Meeting audio capture.** For meeting transcription and translation Rupor captures only the
  audio of one program, such as the browser with the meeting, and not your microphone
  (Windows 11 or Windows Server 2022, build 20348 or later). *All computer sound* is also
  offered and works on Windows 10, where a meeting always listens to it: starting from the
  suggestion card or automatically just works, with one notice that other programs' sounds get
  into the transcript too. There the translation window selects *All computer sound* by
  default and shows programs greyed out with *Needs Windows 11*. Capture runs in a separate
  helper process (the same
  `rupor-core.exe` with `--loopback`), so recognition and AI calls in the engine can't make it
  lose audio. Gaps are filled with silence, so the transcript timing stays correct. The helper
  stops when the engine stops it or exits, and when the captured program closes. The list of
  sources groups each browser's processes into one entry with its friendly name and window
  title, and lists meeting windows first. Volumes are never read or changed.
- **The meeting browser is no longer turned down while you dictate.** Capture happens after
  the program's own volume, so *Computer audio while recording* now leaves the captured
  program alone for the whole meeting; a meeting that listens to all computer sound turns
  nothing down. If a meeting starts during dictation, that program is turned back up at once.
- The build self-check (`--uia-selftest`, `packaging/verify_core.py`) also checks audio
  capture: the activation handler, capture of the engine's own process, and a round trip
  through the `--loopback` helper. Nothing is played. On Windows older than build 20348,
  "capture unavailable" is accepted.

## [0.7.0] — 2026-11-13

### Added

- **Speech page: your filler words.** A new *Speech* page in the sidebar counts filler words
  and phrases ("ну", "короче", "как бы", "типа", "в общем", "эээ", "um", "you know"…) so you
  can work on how you talk. They are counted from the **raw** speech-recognition text of each
  dictation, before the dictionary and AI cleanup (cleanup removes fillers), locally and for
  free. The page shows fillers per 100 words with the trend against the previous period, the
  total, the top filler, a per-day chart and the full top list with counts and per-100 rates,
  for 7 / 30 / 90 days or all time. Stats are kept per day in their own tables, so history
  retention and clearing history don't erase them. The first time the page opens, past
  dictations from history are counted too.
- **Editable filler list.** Add your own words and phrases, remove ones you actually use with
  meaning, restore the defaults, or recount past dictations from history with the new list.
  Drawn-out sounds are merged ("э", "э-э-э", "эмм" → "эээ"; "uhh" → "uh"). Counting can be
  turned off.
- **Find my phrases with AI** (optional, one paid request to your cleanup model): the AI reads up
  to 80 recent dictations from history and suggests habitual phrases that aren't in the list
  yet; click one to add it. Counting itself stays local.
- Note on the page: Whisper often drops "uh/um" and broken-off words by itself, so hesitation
  sounds show up less than in real speech; and the counter doesn't understand meaning ("как бы
  ты поступил" also counts as "как бы").

## [0.6.3] — 2026-11-13

### Fixed

- **"Продолжение следует..." at the end of a dictation.** Whisper learned on subtitles, and on
  the silence at the end of a recording it sometimes "hears" their service phrases and appends
  them to real speech: "Продолжение следует...", "Спасибо за просмотр", "Субтитры сделал
  DimaTorzok", "Thanks for watching". Such phrases are now cut off the end of the recognised
  text (a recording that is nothing but such a phrase was already dropped). The same tail is
  removed if the AI cleanup adds it. A phrase in the middle of the text is left alone.

## [0.6.2] — 2026-11-13

### Fixed

- **"The AI response was cut off at the token limit" on long dictations.** Newer Anthropic
  models (Claude Haiku 5.5 and others) "think" before answering by default, and that thinking
  is taken from the same answer limit. On a dictation of a minute or more the model could use
  the whole limit on thinking and send back only a scrap, so Rupor pasted the raw text without
  cleanup. Cleanup, command mode, meeting translation and meeting notes now turn thinking off on the models that allow
  it (Haiku 5.5, Sonnet 5, Opus 5); on the ones that can't (Opus 5.5, Sonnet 5.5, Fable) they ask
  for low effort and add 4096 tokens of headroom. The answer limit is now at least 1024 tokens,
  and if an answer is still cut off, cleanup retries once with a four times larger limit; only
  if that fails too is the raw text pasted, as before. You pay only for tokens actually
  written, so the larger limit costs nothing extra.

## [0.6.1] — 2026-11-08

### Fixed

- **Recognition no longer gets stuck repeating a phrase.** Whisper sometimes looped on a short
  phrase in the middle of a normal dictation ("which interface, which interface, which
  interface…") and kept repeating it to the end instead of what was actually said; such a loop
  was too short for the old repetition check. Now a phrase repeated three times in a row (a
  single word four times) makes Rupor recognise the dictation once more without the hint, and
  that pass is used when it is clean. If the repeat is still there, it was most likely really
  said and is kept; only repeats no person makes (a phrase four times or more, a word six
  times) are collapsed to one.

### Added

- **Transcribe again from the History feed.** Every dictation that still has its voice
  recording now has a *Transcribe again* button right next to it, not only under History →
  Recordings. The new text appears at the top of the feed and in *Paste last*.

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

[0.6.1]: https://github.com/listapad2024/rupor/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/listapad2024/rupor/compare/v0.4.3...v0.6.0
[0.4.3]: https://github.com/listapad2024/rupor/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/listapad2024/rupor/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/listapad2024/rupor/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/listapad2024/rupor/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/listapad2024/rupor/releases/tag/v0.3.0
