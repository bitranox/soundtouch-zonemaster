# Key mappings

What each key on a SoundTouch remote or speaker does while `soundtouch-zonemaster-service` holds the
house. The prototype master (`soundtouch-zonemaster`) reads no keys at all.

**A key does not have one meaning.** What it does depends on the state of the box it was pressed at
and of the house around it, so every table below is keyed by state as well as by key. The states that
matter are listed first, and each section says which of them it depends on.

## The states that change what a key means

| State               | Values                          | Decided by                                             |
|---------------------|---------------------------------|--------------------------------------------------------|
| The switch          | on / off                        | the switch file; off means nobody holds the house      |
| The house           | playing / quiet                 | whether the zone master is playing a channel right now |
| The box             | asleep (STANDBY) / awake        | what the box last reported about itself                |
| The box's place     | a zone slave / outside the zone | whether the master has taken the box in                |
| Multiroom           | in / out                        | a double-tapped thumb; kept in the state file          |
| The channel playing | radio / MPD                     | the `kind` of the channel in the channel file          |
| A calibration       | running / not running           | the calibration gesture, below                         |
| The gesture         | tap / hold                      | whether the key came back up inside the hold threshold |

Two facts about the hardware sit under every one of them:

- **Next, previous and the thumbs reach the master only as `POST /slaveMsg`** with a `keyData`
  element, which a box sends over its connection to the master. A zone slave has that connection.
  A box outside the zone has none, so its keys never reach the service - and that includes a box
  somebody took out of multiroom, which is released from the zone (see
  [getting a box back](#thumbs-up-and-thumbs-down)). Outside the zone a box reports only THAT a key
  went down and came up, never which one. Preset keys are different: the service reads those from
  every box's own notification WebSocket, in the zone or not.
- **A preset key has no hold.** It arrives as a selection plus an "a person touched this box" frame,
  with no press and release of its own, so a long `1` is a short `1`. The other four keys arrive
  twice, as `press` and `release`, which is what makes a hold measurable for them.

## The dialling window

How long the house waits after a key before it reads the digits or steps as one number or jump.

- Default 0.8 s, bounds 0.5 to 2.0 s, set as `[dialling] window_s`.
- It is armed when a key comes back UP, not when it goes down, so the time a thumb rests on a key is
  not charged against the next one.
- A calibrated window (see [Calibration](#calibration-next-previous-next-previous)) is written to the
  state file and beats every configuration layer and the command line.

## The hold threshold

How long a key must stay down to be a hold rather than a tap. It is a number of its own because it
answers a different question: the window is the pause BETWEEN two keys, the threshold is how long
ONE key is down, and a slow tap can be down longer than the pause after it.

- Default 1.0 s, bounds 1.0 to 2.0 s, set as `[dialling] hold_threshold_s`.
- A key still down when it passes is a hold and acts at that moment, not when it is let go.
- The same calibration measures it, and a calibrated value beats every configuration layer too.

## Preset keys 1 to 6: dialling

The six preset keys are the digits of a channel number. Channel numbers use the digits 1 to 6 only:
1 to 6, then 11 to 16, 21 to 26 and on to 66, then 111 upwards. Repeated digits are real numbers, so
`1` `1` is channel 11. Digits pressed inside one window form one number; the number is read once the
window has passed with nothing further pressed.

What a completed number does depends on the box and the house:

| Box                                                          | House                    | A preset press, or a dialled number                                                                                               |
|--------------------------------------------------------------|--------------------------|-----------------------------------------------------------------------------------------------------------------------------------|
| awake, in multiroom                                          | switch on                | The WHOLE zone switches to that channel, and the box that dialled is taken into the zone if it was not in already.                |
| asleep, in multiroom                                         | quiet (nothing playing)  | Wakes the box, and the number chooses the channel the house starts on.                                                            |
| asleep, in multiroom                                         | playing                  | Wakes the box and takes it into the zone on the channel already playing. The digit is NOT dialled.                                |
| asleep, in multiroom, woken onto no preset of its own (id 0) | either                   | Taken as a wake: the box joins the zone.                                                                                          |
| out of multiroom, awake                                      | either                   | That box alone plays the channel. The house keeps playing what it was, and its remembered channel is not changed.                 |
| out of multiroom, asleep                                     | either                   | Switching a box on is the way back: it is in multiroom again, and the press then does what it does at an asleep box in multiroom. |
| in multiroom                                                 | switch off               | The number is remembered and becomes the channel the house starts on when the switch is turned on.                                |
| any other box                                                | a calibration is running | Ignored, and logged as pressed while another box is calibrating.                                                                  |
| the box being calibrated                                     | a calibration is running | Not dialled: the press is a calibration sample.                                                                                   |

Two outcomes are deliberately nothing at all, so that a mistyped number is harmless:

- A number that no channel has does nothing, and the log says so.
- Dialling the channel that is already playing does nothing. It is compared by channel NUMBER, so
  moving between two MPD channels (which share one stream address) is a real change.

A box that is out of multiroom still reaches every channel in the list, including the ones past the
six its own keys can hold. One limit: MPD has a single output, so an MPD channel dialled at a box
that is out plays whatever MPD is playing for the house, not that channel's playlist.

### Holding a preset key: the speaker's own meaning

Holding a preset key for about two seconds makes the speaker's firmware STORE whatever is playing
into that preset. The master cannot see the hold and reads it as an ordinary tap of that digit.
Measured on real boxes, a store has also emptied or rewritten a different preset slot on the same
box, with no rule that fits every case. The house's channel list is seeded from a box's presets on
first start, so avoid holding preset keys on a box whose presets you care about.

## Next and previous

Both keys reach the master only over a box's connection to it (see above).

**Tap.** Collected on the way down: every tap inside the window adds one step (next +1, previous
-1), and the collected steps are taken as ONE jump when the window closes. Next then previous inside
one window sums to zero and does nothing.

What a jump moves through depends on the channel playing:

| Channel playing | Next / previous                                                                                                                                                              |
|-----------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| radio           | The next or previous channel in the ROTATION, in dialling order, wrapping at both ends. The whole zone changes channel.                                                      |
| MPD             | The next or previous FILE of that channel's playlist or directory, one file per collected press, wrapping at both ends. The channel does not change and no room goes silent. |

Consequences worth knowing:

- On an MPD channel, next and previous never leave the channel. Dial a number to leave it.
- Next on the last file goes to the first and previous on the first goes to the last, on every MPD
  channel. That holds for a channel whose `end` is `stop` too: `stop` is only for the end the
  playlist reaches by itself (see [The end of an MPD channel](#the-end-of-an-mpd-channel)).
- Stepping through radio channels can land on an MPD channel, and from there the keys page files.
- Stepping needs at least two channels in the rotation; with fewer the log says there is nowhere to
  step to.
- Stepping from a channel that was taken out of the rotation goes to its neighbours in the rotation,
  not to an end of the list.

**Hold.** A next or previous still down when the hold threshold passes acts at that moment. What it
does depends on the channel playing:

| Channel playing | Held next / held previous                                                                                                                                                                                          |
|-----------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| radio           | The same as one tap: the next or previous channel.                                                                                                                                                                 |
| MPD             | A whole DIRECTORY: held next goes to the first file of the next directory in play order, a subdirectory included. Held previous goes to the first file of the directory playing, and from there to the one before. |

On an MPD channel a held key wraps at both ends like a tap, and it works on a stored playlist too,
whose files can come from several directories. Two holds are two directories. What was collected on
the way down is dropped, so a hold acts once, and a step never acts while its key is still down, so
a slow tap is one step too.

**While a calibration runs**, both keys do nothing and the log says so.

### The end of an MPD channel

Each MPD channel says in the channel file what happens when its playlist runs out by itself:

| `end` in the channel file | At the natural end                                                                                                    |
|---------------------------|-----------------------------------------------------------------------------------------------------------------------|
| `wrap`, or no `end`       | Plays on from the first file (MPD `repeat` on). Right for music.                                                      |
| `stop`                    | Falls silent, and the remembered place is forgotten, so the next time somebody dials it the book starts from the top. |

A radio channel has no end, and `"end": "stop"` on one is refused when the file is read. The place
is forgotten at the moment the house leaves the channel, or when the switch goes off, so a service
restarted in between still has the old place.

### Channels that play a directory

An MPD channel names either a stored playlist (`mpd_entry`) or a directory (`mpd_directory`), never
both. A directory is a path under MPD's music directory, for example
`"mpd_directory": "audiobooks/Author Two/Book Two"`, and the channel plays every file under it,
subdirectories included, in this order:

- On every level, the files of that level first, then its subdirectories, each played to its end
  before the next one starts. So `Buch/01.mp3`, `Buch/02.mp3`, then `Buch/Bonus/01.mp3`.
- Names compare naturally: `Kapitel 2` before `Kapitel 10`, case does not matter, and an umlaut
  sorts with its base letter. The order is the same on every machine.

The place a directory channel was left is remembered by the FILE'S NAME, so adding a file to the
directory does not move the house into another chapter. If the file itself is gone, the channel
starts at the same position in the list, from the beginning of the file now there.

A directory MPD does not have, or one with nothing MPD can play in it, is said by name in the log,
and the channel is silent.

## Thumbs up and thumbs down

Both keys reach the master only over a box's connection to it (see above). A thumb acts when it is let go (a
tap) or when the hold threshold passes with it still down (a hold); nothing is collected at the press,
because the gestures mean unrelated things. Two taps of the same thumb are a double tap when the
second goes down within the dialling window of the first coming up; a third tap starts again.

| Gesture    | Thumbs up                                                                                                             | Thumbs down                                                                         |
|------------|-----------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------|
| tap        | The volume keys set the whole house for the next few seconds ([house volume](#house-volume-tap-a-thumb-then-volume)). | The same as thumbs up: the house volume.                                            |
| double tap | At a box still attached but out of multiroom: puts it back in (below). At a box already in: logged, nothing else.     | Takes THIS box out of multiroom: it is released from the zone and plays on its own. |
| hold       | Puts the channel that is playing back into the rotation.                                                              | Takes the channel that is playing out of the rotation.                              |

The rotation takes a HOLD because it changes the whole house: a single stray touch must not take
a channel out for everybody with nothing audible to say so.

The rotation is what next and previous walk through. It is stored in the channel file, so a hold
changes it for the whole house. A channel out of the rotation keeps its number and can still be
dialled, which is how a stray thumbs down is undone from the room. The last channel in the rotation
cannot be taken out: the press is refused and logged.

Multiroom is per box and is stored in the state file, so it survives a restart. A box taken out is
released from the zone - the master sends it `/removeZoneSlave`, which also puts it into standby -
and at once handed the channel the house was playing, so the room is silent for about a second
and then plays on by itself. From then on its preset keys dial for it alone (see the dialling
table).

**Getting a box back: switch it off and on again.** A released box has no connection to the master
and reports its keys only as anonymous touches (measured 2026-09-24), so a double thumbs up from it
never arrives. Switching it on - the power key, or a preset key while it is in standby - puts it
back into multiroom, and it joins the zone like any box that wakes. The service's own wake of the
box, the station it hands it right after the release, does not count. A double thumbs up still
brings back a box the release did not reach, since that box is still a zone slave. As a last resort
the flag is the box's device id in the state file's `out_of_multiroom` list; edit that only while
the service is stopped, because the service reads the file at start and writes it back whenever its
state changes.

Pressing a thumb in a state where it changes nothing (a channel already in the rotation) is logged
and does nothing. **While a calibration runs**, both thumbs do nothing.

## House volume: tap a thumb, then volume

At a box that is playing in the zone:

1. **Tap thumbs up or thumbs down** once.
2. **Use volume up or down** at the same box, tapped or held, within 4 s of the thumb coming up.

A second tap of the same thumb makes it a double tap, which moves the box in or out of the group
and hands the volume keys back to the room.

Every other box then moves by the **same step** as the box you are at, each from its own level, so
a room that was quieter stays quieter; a box stops at 0 and at 100. Every volume report from your
box keeps this going for another 3 s, so you can tap, listen and tap again. After that the volume
keys are that room's own again.

- **Two keys at once do not work.** The remote sends only the key held first (measured 2026-09-24,
  `docs/measurements/2026-09-24-thumb-and-volume.md`), so the thumb has to come first.
- **A box that is off, or on another input, is not written.** It is owed the step instead, in the
  state file, and takes it when it next joins: the join turns it down and fades it back up to its
  own level plus what it missed. A box owed below zero joins at zero.
- **A box out of multiroom takes nothing.** It left the house on purpose.
- **A box still fading in when you turn the house** ends its fade at the moved level.

## Calibration: next, previous, next, previous

The dialling window and the hold threshold can be measured on the person who uses them instead of
typed into a file.

1. **Start**: at a zone slave, press next, previous, next, previous, alternating, all four within
   4 s. Two of the same key in a row starts the count again. Any other key in between breaks it.
2. **Heard**: the channel playing starts again. That break in the music is the signal that the
   calibration began.
3. **Measure**: at the same box, press preset keys the way you would dial a number. Every press is a
   sample and nothing is dialled. Presses at other boxes are ignored, and next, previous and the
   thumbs do nothing until it ends.
4. **End**: 3 s after the last press, or 30 s after the start, whichever comes first. The channel
   starts again once more, whichever way it went.

The window becomes the longest gap between two presses (from one key coming up to the next going
down) plus a margin of at least 0.1 s or a quarter of that gap, rounded up to a tenth and clamped to
0.5 to 2.0 s. Gaps over 2 s count as pauses, not samples. Fewer than three usable gaps (four presses)
leaves the window unchanged.

The hold threshold becomes the longest time a key was DOWN in the same presses, with the same margin
and rounding, clamped to 1.0 to 2.0 s. With fewer than three usable presses it stays as it was. Both
results are saved in the state file, and the log line names both.

Pressed slowly, the four steps of the gesture can each leave the window before the next one lands,
and the zone then steps a channel and steps back. The listener ends where they started, having heard
two restarts.

## Keys the service does not handle

These keep the speaker's own meaning. Some of them still change who is in the zone, because the
service decides membership from what each box reports:

| Key                     | What happens                                                                                                                                                                |
|-------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Power off               | The box goes to STANDBY and leaves the zone.                                                                                                                                |
| Power on                | The box wakes. If it plays internet radio it is taken into the zone on the house's channel; a preset press does the same. A box that was out of multiroom is back in.       |
| Source (AUX, Bluetooth) | The box leaves the zone and is left alone, so nobody listening on an input is overridden.                                                                                   |
| Volume, mute            | The speaker's own volume. Not forwarded to the master, which only hears the level the box reports - and after a thumb tapped once that level steps the whole house (above). |
| Preset held (about 2 s) | The firmware stores the playing station into that preset. See the warning under the preset keys.                                                                            |

## Where this lives in the code

| Rule                                        | Module                                                            |
|---------------------------------------------|-------------------------------------------------------------------|
| Which key is a step, which thumb is which   | `application/zone_service/dialling.py` (`STEP_OF`, `ROTATION_OF`) |
| Tap or hold                                 | `domain/longpress.py`                                             |
| Digits into a number, steps into a jump     | `domain/dialling.py`                                              |
| The dialable numbers, the ladder, rotation  | `domain/channellist.py`                                           |
| A press or our own echo                     | `domain/presses.py`                                               |
| The calibration gesture and its statistic   | `domain/calibration.py`                                           |
| House volume: the step, the window, owing   | `domain/housevolume.py`                                           |
| House volume: who is written, noted, owed   | `application/zone_service/volume.py` (`_house_stepped`)           |
| What a key does in which state              | `application/zone_service/keys.py`, `dialling.py`, `channels.py`  |
| Who belongs in the zone                     | `domain/membership.py`                                            |
| A directory's play order, a held key's jump | `domain/playorder.py`                                             |
| A remembered place found by file name       | `domain/state.py` (`Place.found_in`)                              |

The measurements behind these rules are in `research/REPORT.md` and `docs/measurements/`, which
are kept on the development machine and published in anonymized form when development is finished.
