# Student guide

You write one Python file. It contains a class with one method, `schedule`,
which is called every time something happens in the kitchen and answers one
question: *which order should each cook be working on right now?* Everything
else is done for you.

---

## 1. Set up

Download the platform from the **Downloads** tab on the course site. It is one
zip, and it already contains the simulation engine compiled for Windows, macOS
and Linux. **There is nothing to build and nothing to install** - you need
Python 3.8 or newer and nothing else.

This package is not on PyPI, and there is no `pip install` step. If you find
yourself installing a compiler you are on the wrong path; go back to the zip.

Unzip it somewhere you will find again, and open that folder. Everything in this
guide happens inside it.

| You are on | Start it with |
| --- | --- |
| Windows | double-click `START.cmd` |
| macOS or Linux | `bash start.sh`, in a terminal opened in that folder |

Either one opens the dashboard in your browser: pick a scheduler and a service,
press **Play & watch**, and watch the kitchen. That is the whole loop.

What is in the folder:

```text
my-scheduler/  your scheduler - scheduler.py. That file IS your submission.
configs/       the service profiles: menus, loads and rules
docs/          this guide
replays/       runs you record land here
START.cmd      Windows
start.sh       macOS and Linux
```

`my-scheduler/scheduler.py` is already a working scheduler, so you can change
one thing and see immediately whether it helped.

**Throughout this guide, commands are written `./start.sh <something>`. On
Windows type `START.cmd <something>` instead - the arguments are identical.**
Check that it works:

```bash
./start.sh play my-scheduler
```

You should see a score and a table of numbers. If you do, everything is in
place. If you do not, `./start.sh doctor` says what it found.

---

## 2. The picture

The kitchen is a computer and you are its scheduler.

| In the kitchen | In the operating system |
| --- | --- |
| a cook | a CPU core |
| an order | a process |
| a recipe's `work` steps (chop, sear, plate) | CPU bursts - the cook is busy for every tick of them |
| a recipe's `wait` steps (oven, simmer, rest) | I/O bursts - the dish needs nobody; the cook is free |
| a cook taking on an order | a context switch, costing `switch_cost` ticks before any work happens |
| a station (the bench, the pass, the bar) | a lock with a fixed number of permits - a work step has to hold one to run |
| the customer's patience | a deadline - at `deadline` an unserved customer walks out |
| the ticket rail | the ready queue |
| you | the scheduler |

Time is in **ticks**. Every order arrives at a tick, has a recipe of steps with
durations in ticks, and a deadline tick. An order is **ready** when it is
waiting for a cook, **running** when a cook holds it (including while that cook
is still paying the switch), and **blocked** when it is in the oven. Nobody can
be assigned an order that is in the oven.

You are called at **scheduling points**: when service opens, when an order
arrives, when a cook finishes a step (the order is served, or goes into the
oven and the cook is free), when an order comes back out of the oven, when a
customer walks out - and when an alarm you set goes off. Between scheduling
points the kitchen runs on its own, and nothing you decided changes.

Three rules give the game its shape:

1. **Switching costs.** Every time a cook takes on an order - from idle, or
   instead of what it was doing - it spends `switch_cost` ticks before any work
   happens. Taking an order off a cook mid-step keeps the step's progress, but
   somebody will pay a switch to resume it. Changing your mind is not free.
2. **Customers leave.** An order not served by its `deadline` is *abandoned*:
   the customer walks out, and every tick a cook spent on it was wasted. It does
   not matter how nearly done it was.
3. **Stations are shared.** Every work step happens somewhere, and only so many
   cooks fit there. A cook needs a free place at a step's station before it can
   start it, and holds that place for as long as it holds the order. You are
   allocating two things at once: cooks, and places. See §4.

---

### Stations

A cook is not the only thing an order needs. Every work step names a
**station** - the prep bench, the pass, the bar - and each station has a fixed
number of places at it:

```
[[station]]
name = "pass"
capacity = 1      # one cook can plate at a time. That is all.
```

That is a counting semaphore with `capacity` permits, and the rule is short:

> A cook holds one place at the station of the step it is on, from the moment
> it is dispatched until it lets the order go. **An assignment to an order
> whose station is full is refused.**

Refused, not queued. The assignment is counted as invalid, ignored, and the
cook stays idle for that decision - so if you do not check, you are throwing
away cooks. `obs.stations` is right there, and every reference scheduler checks
it before its actual policy gets a say:

```python
free = obs.free_stations()            # {"board": 2, "pass": 0, "bar": 1}
for core in obs.idle_cores:
    for order in queue:
        if order.station is None or free.get(order.station, 0) > 0:
            decision.assign(core, order)
            if order.station:
                free[order.station] -= 1     # you just spent a place
            break
```

`helpers.fill_idle` does exactly this, so the shortest correct scheduler is
still three lines. What is *not* done for you is the interesting half: which
order to prefer when the station you want is busy, and how to avoid getting
into that position in the first place.

Three consequences worth knowing:

* **A cook only ever holds one station, and always lets go before it takes the
  next.** No hold-and-wait, therefore no deadlock - ever. This kitchen cannot
  hang. It can starve and convoy beautifully, and that is what you are up
  against.
* **Preempting a cook frees its place.** If you take an order off a cook in the
  same decision, that place is available to whatever takes over. Add it back to
  your `free` dict when you do.
* **An order can be bumped.** When a work step ends and the next work step is
  at a *different* station that happens to be full, the order goes back on the
  rail rather than hold a cook hostage waiting. You get called (`reason.bumped`),
  the cook is idle, and resuming that order will cost a switch. Salad is the
  one that does this: chop at the bench, dress at the pass.

Waits hold nothing. A dish in the oven needs no cook and no station - which is
exactly why overlapping them matters.

| | |
| --- | --- |
| `obs.stations` | every station: `name`, `capacity`, `busy`, `free`, `is_full`, `cores` |
| `obs.station(name)` | one of them |
| `obs.free_stations()` | `{name: free}`, a plain dict you can spend as you build a decision |
| `obs.can_start(order)` | whether that order's station has a place right now |
| `obs.runnable` | `obs.ready` with the ones that cannot start removed |
| `order.station` | where the step it is on happens, or `None` |
| `step.station` | where any step happens; always `None` for a wait |
| `core.station` | where a cook is standing - the place it will hand back |

A profile with no stations turns all of this off; `order.station` is then
`None` everywhere and nothing is ever refused.

---

## 3. The interface

```python
from kitchen import Decision, Scheduler


class MyScheduler(Scheduler):
    name = "my_scheduler"
    version = "1"

    def reset(self, seed):
        """Optional. Once per run, before the first call."""

    def schedule(self, obs):
        """Required. Called at every scheduling point.

        Takes an Observation, returns a Decision.
        """
        return Decision()
```

That is the whole interface, and it runs - but an empty `Decision` names no
cook, so every cook carries on with whatever it had, which at the start of
service is nothing. It scores zero. `schedule` is the only method you must
write; `reset` and `on_run_end(result)` are optional. `name` appears in your
own results and replays; the leaderboard uses the identity Moodle has for you.

**The filled-in version of exactly this is the `my-scheduler/scheduler.py` you
were given.** Read it before you change it: it is the only scheduler in this
course whose source you get, and its comments explain each move it makes. The
reference schedulers are compiled into the engine, so you can play them,
measure them and read what algorithm each one implements - but not read their
code. Working out how they do it is the assignment.

`fill_idle(decision, obs, queue)` gives each idle cook the next order from
`queue`, in order, and returns whatever was left. It is three lines long and
you will outgrow it; it is there because every scheduler starts with exactly
that loop.

---

## 4. What you can see

`obs` is the whole kitchen at this instant. It reports facts, never advice:
nothing in it says who should go next.

### The kitchen and the clock

| | |
| --- | --- |
| `obs.time` | now, in ticks |
| `obs.kitchen.cores` | how many cooks |
| `obs.kitchen.switch_cost` | ticks a cook loses every time it takes on an order |
| `obs.kitchen.max_ticks` | when service ends whatever is still cooking |
| `obs.kitchen.known_durations` | whether you are told how long steps take (see §4, *In the dark*) |
| `obs.kitchen.slowdown_bound` | the floor the slowdown component divides by (see §6) |

**There is no `obs.kitchen.name`.** It was there until 10 September and it has
been taken out, because it let a scheduler ask *which profile is this* and
look up an answer instead of working one out. Everything left in this table is
a property of the kitchen you can act on, and a scheduler that reads them all
never needs to know the profile's name. Reading the workload - how many cooks,
how loaded the rail is, how much of the work is oven time - is scheduling, and
it goes on being worth marks. Recognising the workload is not.

### Why you were called

`obs.reason` is the list of things that happened since your last decision.

| | |
| --- | --- |
| `obs.reason.kinds` | any of `"start"`, `"arrived"`, `"blocked"`, `"unblocked"`, `"served"`, `"abandoned"`, `"timer"` |
| `obs.reason.arrived` | ids of orders that just arrived |
| `obs.reason.unblocked` | ids just back from the oven |
| `obs.reason.bumped` | ids sent back to the rail because the station their next step needed was full |
| `obs.reason.blocked`, `.served`, `.abandoned` | ids that just did those things |
| `obs.reason.timer` | whether your alarm went off |
| `"arrived" in obs.reason` | works too |

Most schedulers do not need to look at this; the state below is complete on
its own. It is there for the ones that keep their own queue.

### Orders

`obs.orders` is every live order, oldest first. `obs.ready`, `obs.running` and
`obs.blocked` are the three subsets. `obs.order(id)` finds one by id.

| `order.` | |
| --- | --- |
| `id` | a number; ids are assigned in arrival order |
| `recipe` | `"espresso"`, `"burger"`, ... |
| `priority` | 1 a regular customer, 2 a hurried one, 3 a VIP. **The engine does nothing with this**; what you do with it is up to you |
| `arrival` | the tick it arrived |
| `deadline` | the tick the customer walks out |
| `time_left` | `deadline - now` |
| `state` | `"ready"`, `"running"` or `"blocked"`; also `is_ready`, `is_running`, `is_blocked` |
| `core` | the cook holding it, or `None` |
| `steps` | the recipe: a list of `Step`s with `label`, `kind` (`"work"`/`"wait"`), `duration`, `remaining`, `done` |
| `step` | index of the current step; `current_step` and `steps_left` are the convenient views |
| `work_remaining` | ticks of cook time still needed, across every remaining work step |
| `work_until_wait` | ticks of cook time before it next goes into the oven (or finishes) |
| `work_done` | ticks of cook time already spent on it |
| `waited` | ticks it has spent on the rail |
| `started` | the tick a cook first worked on it, or `None` |
| `preemptions` | times it was taken off a cook mid-step |
| `dispatches` | times a cook took it on |

### Cooks

`obs.cores` is every cook; `obs.idle_cores`, `obs.working_cores` and
`obs.busy_cores` are the subsets; `obs.core(id)` finds one; `obs.order_on(core)`
is the order it holds.

| `core.` | |
| --- | --- |
| `id` | 0 upwards |
| `state` | `"idle"`, `"switching"` (paying the switch) or `"working"`; also `is_idle`, `is_switching`, `is_working` |
| `order` | the id it holds, or `None` |
| `station` | the station it is standing at, or `None` - freed if you take its order |
| `switch_remaining` | ticks of switch still to pay |
| `running_for` | consecutive ticks of work on the current order - the time slice used so far |

### The menu, and what has been served

| | |
| --- | --- |
| `obs.recipes` | the menu by name: each recipe's `priority`, `patience` range, and `steps` as `(label, kind, [min, max])` |
| `obs.history` | the last sixty served orders: `recipe`, `total_work`, `turnaround`, and every step's real duration |
| `obs.stats` | running totals: `served`, `abandoned`, `switches`, `work_ticks`, `idle_ticks`, ... |

### In the dark

Some profiles - `blind` is one - hide durations. Then `order.work_remaining` is
`None`, a step's `duration` and `remaining` are `None` until it is finished,
and the menu shows the shape of each recipe but not its ranges. That is the
position a real scheduler is in: it never knows how long a burst will take.

`obs.estimate_remaining(order)` is a guess you can use: the true figure when it
is known, otherwise the mean total work of served orders of the same recipe
(from `obs.history`), less the work already done. The reference schedulers use
exactly this. Doing better than it - a per-step estimate, an exponential
average, a per-recipe model - is one of the ways to beat them.

---

## 5. What you can do

A `Decision` names cooks. A cook you do not name **carries on** with what it
was doing. An order you do not name **waits**.

```python
d = Decision()
d.assign(core, order)     # put this order on this cook; ids or objects, either works
d.idle(core)              # take this cook off its order; the order goes back on the rail
d.wake_in(8)              # call me again in 8 ticks even if nothing happens
d.annotate(text="SJF", queue=[o.id for o in my_queue])   # drawn by the viewer, ignored by the engine
return d
```

A plain dict of the same shape is accepted too: `{"assign": {0: 17, 1: None},
"timer": 8}`.

What happens when you assign:

- **A cook already on that order** - nothing changes and no switch is paid.
- **A cook on a different order** - that order goes back to the rail *with its
  progress kept* (a preemption), and the cook starts switching to yours.
- **An idle cook** - it starts switching. Work begins `switch_cost` ticks later.
- **`None`** - the cook drops what it holds and stands idle.

You may move an order from one cook to another in a single decision; the
engine lets go before it dispatches.

Things the engine will not do, and counts instead: assign an order that is in
the oven, that was already served, that has not arrived, or that does not
exist; assign an order whose **station is full**; name a cook twice; put one
order on two cooks. Each is **rejected**, the rest of your decision still
applies, and the rejection is counted and shown to you (`./start.sh check`
reports them). They cost you nothing directly, but they mean your scheduler
asked for something impossible, and the cook it meant to use stays as it was -
which on a profile with a tight station is most of the marks.

### The alarm

You are only called when something happens. A time slice running out is not
something that happens on its own - so if you want round robin, or you want to
revisit a decision when a customer is about to give up, set an alarm:
`wake_in(n)` calls you again in `n` ticks (an arrival or a completion before
then still calls you, and the alarm still fires afterwards). `wake_in(0)`
cancels it. Not setting it leaves whatever alarm you set before.

### Showing your working

`annotate` attaches notes to a decision. `text` is a line about this decision.
`queue` is the order you would serve the rail in, and the viewer draws the rail
in that order, so you can *see* your policy rather than infer it from the cooks'
movements. `tags` labels individual orders (`{order.id: "skipped: cannot
finish"}`). It is size-limited and silently dropped if it grows too large, it is
recorded only when a replay is, and it is ignored during marking - so leaving it
in costs you nothing, and the starter you were given already uses it.

---

## 6. What you are marked on

Six things, each scored between 0 and 1 and weighted into a score out of 100.
The weights, and every raw number behind them, are printed with every run.

| Component | Weight | What it measures | The formula |
| --- | ---: | --- | --- |
| **completion** | 30 | how many customers you served before they left | `served / orders` |
| **response** | 12.5 | how quickly an order first got a cook | `1 / (1 + mean_response / 25)`, over every order that arrived |
| **turnaround** | 12.5 | how long a customer was in the room, start to finish | `1 / (1 + mean(left − arrived) / 60)`, over every order that finished or walked out |
| **slowdown** | 15 | how long orders took, *relative to the time they needed* | `1 / mean(turnaround / max(span, 10))`, over every order that finished or walked out |
| **switching** | 15 | how much switching was *your* choice | `necessary switches / all switches` |
| **fairness** | 15 | how evenly the slowdown was shared | Jain's index over every order's slowdown |

The score is
`100 × Σ(component × weight) ÷ Σ weights`. The weights are divided by their own
total, so they are ratios rather than percentages - they happen to sum to 1 so
the table reads as marks out of a hundred, and a profile that re-weights them
does not have to make them sum to anything. A forfeited run scores zero
whatever the components say.

Three of the six are latency, measured three different ways, and it is worth
being clear about the difference because a scheduler can win one and lose
another:

```
        arrival                first work                        served
           |------ response ------|------------------------------|
           |------------------ turnaround -----------------------|
                                    slowdown = turnaround / max(work, 10)
```

**Response** stops at the first tick of work - it is how long before *anything*
happens. **Turnaround** is the whole stay, in ticks: every oven wait, every
tick back on the rail after a preemption, all of it. **Slowdown** is turnaround
divided by the time the order *needed* - its work plus its oven waits - so a
two-tick espresso that took twenty is punished far more than a sixty-tick
banquet that took eighty. Oven time is in the denominator as well as the
numerator on purpose: a roast needs a hundred ticks in the oven whoever is
cooking, and you are not marked down for time nobody could have saved. An
order served the moment it arrives scores 1; everything above 1 is queueing,
and queueing is yours. Absolute and
relative disagree constantly: `portion_control` has the best response of any reference
and a middling turnaround, `trainee` has a good turnaround and the worst response
in the table.

Some things worth knowing about them:

- **Completion is the big one**, and abandoned orders hurt twice: the customer
  is gone, and the cook time spent on them was wasted. A dish that cannot be
  finished before `time_left` runs out is not worth starting.
- **Response** counts from arrival to the first tick of *work*, so the switch
  is included: on this kitchen the best possible response is `switch_cost`.
  Orders that never got a cook are not in the mean, but they are counted, and
  they cost you completion.
- **Turnaround** is `completed − arrived`, over served orders only. An order
  that walked out has no turnaround - it costs you completion instead, which is
  worth more. Because it is absolute, the way to move it is to stop orders
  sitting around: it is the component that notices a dish left in the oven
  nobody collected, or an order preempted and then forgotten.
- **Slowdown** is *bounded*: any order needing less than ten ticks counts as
  ten, so a two-tick espresso that waited twelve ticks is a slowdown of 2.2,
  not 7. `obs.kitchen.slowdown_bound` tells you the figure rather than making
  you assume it. It
  rewards getting short jobs through quickly all the same - and it is the
  number that a long job stuck behind other long jobs makes worse.
- **Switching** counts a switch as *necessary* when the cook takes on a work
  step nobody had started; every other switch is a resumption after a
  preemption, and those are the ones you chose. A scheduler that never
  preempts scores 1 here. Round robin with a short quantum sits under a half.
- **Fairness** uses Jain's index: 1 when every order was slowed by the same
  factor, falling as some are made to wait far longer than others. Starving
  the banquet to serve espressos shows up here. Note what it does *not*
  measure: a kitchen that keeps the whole room waiting equally scores well on
  fairness. It is evenness, not welfare - that is what completion is for.

**A customer who walks out is still counted.** Response, turnaround, slowdown
and fairness all cover every order that arrived, not only the ones you served:
an order the customer gave up on counts at the time they sat there and the
slowdown they suffered. Giving up on a hard order therefore costs you on all
five components, not just completion. There is no version of this kitchen
where turning custom away improves your marks.

Twelve reference schedulers are built into the engine as things to measure
against. Their names are kitchen characters and tell you nothing about what
each one does - the right-hand column is what to look up:

| name | the algorithm it is |
|------|---------------------|
| `middle_management` | `fifo` - first come, first served |
| `headless_chicken` | `round_robin` - eight-tick time slices |
| `maitre_d` | `sjf` - shortest job first |
| `sous_chef` | `srtf` - shortest remaining time first |
| `stickler` | `priority` - highest priority first |
| `trainee` | `edf` - earliest deadline first |
| `portion_control` | `stride` - proportional share |
| `smoke_break` | `mlfq` - multi-level feedback queues |
| `julia_child` | the reference that used to be strongest, and still hard to beat |
| `gordon_ramsay` | the strongest reference, and the one to beat |
| `idiot_sandwich` | the starter you were given: longest job first, preempting for anything longer |
| `work_experience`, `dishwasher` | the floors: random, and never cooking at all |

The name in the right-hand column is the one to look up in a textbook, and it
also works anywhere a scheduler is named - `./start.sh play sjf` and
`./start.sh play maitre_d` are the same run.

`./start.sh compare my-scheduler` puts yours in the same table as all of them,
on the same seeds - and gives you a **badge for every one you out-scored**,
plus one for each column you led the field on.
`idiot_sandwich` is on that table too, so "am I beating what I was handed?"
is a question you can answer at a glance - it is the same scheduler as the one
in your `scheduler.py` before you touched it.
Beating `dishwasher` is a bronze shield and beating `gordon_ramsay` is a blue one; the
tiers are a claim about difficulty, and difficulty here is measured rather than
guessed. The same badges appear next to your name in the standings once the
marker has run, against whichever references were entered in that run.

`portion_control` is worth reading before you write anything: it is proportional share
without a random number in it. Every order holds tickets equal to its priority
and a *stride* inversely proportional to them; the kitchen always works on the
lowest accumulated *pass*, and charges an order its stride for every tick of
cook time it gets. A VIP advances a third as fast per tick and so comes up
three times as often, and over a service the cook time splits in exactly the
ratio of the tickets. It has the best response time of any reference here.

Read that table before you write anything, and read it as evidence rather than
as a ranking. Each of these is a textbook policy you can look up, and the
spread between them is wide - the bottom of the table scores less than half
what the top does. Ask why. Two of them differ by one idea; find the pair and
work out what the idea is worth. One of them is *more* sophisticated than the
one above it and scores *less*; that is not a mistake in the engine, and
working out why is worth more than any single line you could add to your
scheduler.

The top of the table is tight - a point or two separates a good scheduler from
the best one - so the confidence interval matters: a difference smaller than it
is noise, and `evaluate` prints both.

### Profiles

A *profile* is a menu, a load and the rules: how many cooks, what switching
costs, whether durations are shown. They live in `configs/` and the dashboard
lists them. Marking uses several - the course profile `default` ("lunch
service"), and others such as `rush`, `bake-off`, `banquet-night`, `blind`,
`one-cook`, `brigade`, `service-line` and `function` - and your mark is the mean of your
mean score on each, so no single profile can be gamed. Each one's comment says
what it is for. A scheduler that only works when durations are shown will find
out on `blind`; one that does not read `obs.stations` will find out on
`service-line`, where four cooks share a single place at the pass; and one
that never sets an alarm will find out on `function`, where a booked-out
evening arrives in four sittings with four hundred quiet ticks between them
and no dish ever goes in an oven - so between sittings the kitchen stops
asking you anything until a dish is finished, and `wake_in` is the only way to
be asked again. That profile is also weighted differently, because it
is asking a different question: read the comments at the top of its file.

---

## 7. Your first change

You already have a scheduler: `my-scheduler/scheduler.py`, and it is a bad
one. It runs, it is legal, and it loses half the room. Everything from here is
turning that into a good one.

Read it first. Its `schedule` does two separable things, and telling them apart
is most of the battle:

* **machinery** - counting places at the stations, handing orders to idle
  cooks, taking a cook off a dish. This part is finished. `fill_idle` does the
  station counting for you, and the starter's comments explain the one trap in
  it: places already spent do not come back inside the same decision.
* **policy** - *which* order goes next, and *whether* a cook should be taken
  off what it is doing. In the starter this is a single `sorted(...)` call and
  a single comparison, and both of them are wrong on purpose.

So your first change is small: find the `sorted(...)` in `schedule` and change
what it sorts by. `obs` and `order` between them offer a dozen candidates, and
section 4 lists them all - how much work an order still needs, how long its
customer will wait, what it is worth, where it has to be cooked, how far
through it already is. Pick one. Then measure:

```bash
./start.sh play my-scheduler --replay      # and watch what it did
./start.sh compare my-scheduler            # against every reference, same seeds
```

Change one thing, run `compare`, write the number down. Some candidates will
help a lot, some barely, and at least one will be worse than what you started
with - the table is the only way to know which, and the confidence interval is
how you tell a real difference from luck. A scheduler is not an argument you
win by reasoning; it is a claim you check.

Two things make this much easier and neither is part of your policy:

**Annotate your decisions.** `decision.annotate(text=..., queue=[...])` labels
a decision with a sentence and with the order you *claim* you want the rail
served in. The viewer draws both, so you can see whether the kitchen did what
you meant rather than what you typed - which is usually where the bug is. It
is ignored during marking, so it costs you nothing. Section 5 has the details.

**Keep the machinery honest.** `validate` reports `rejected assignments`; if
that number is not zero you are asking for something impossible and dropping
cooks on the floor. It should stay at zero through every change you make.

When sorting alone stops paying, the other half of the job is preempting -
taking a cook off a dish mid-service. Section 5 covers the mechanics and
section 12 asks the questions worth answering about it.

---

## 8. Running your scheduler

### The dashboard

`./start.sh` (no arguments) opens it. Pick a scheduler - yours are listed
first - and a profile, then:

| Button | What it does |
| --- | --- |
| **Play & watch** | runs one service, records it, opens it in the kitchen viewer |
| **Play headless** | the same run, just the numbers |
| **Evaluate over seeds** | mean score with a confidence interval over a seed set |
| **Compare with references** | yours and every reference scheduler, same seeds, one table |
| **Check submission** | what the marker checks: it loads, runs, raises nothing, keeps to the deadline |

The **viewer** shows the rail of tickets (in your `annotate` queue order, if
you gave one) and the kitchen underneath it: one counter per station, with the
grill and the oven along the right-hand wall. Each station has its
places marked out on it - a cook standing in one, or the word `free`, and the
count in the corner. Cooks fetch from the larder on their way in, which is what
the context switch looks like, and carry a finished dish to the oven or the
grill themselves. When every place is taken the counter turns red and the
tickets that need it go grey on the rail with `pass full` on them: that is
precisely the set of orders you are not allowed to start. Dishes on a `wait`
step sit on the grill or in the oven with a timer, holding no cook and no
place, which is exactly what an I/O burst does to a core.

Underneath is the timeline. Each cook gets a row - solid where it worked,
hatched where it was paying a switch - with arrivals and walk-outs marked
above, and the depth of the rail (the ready queue) plotted below. It scrolls
sideways: drag the bar, roll the wheel over it, or change **zoom** to give each
tick more room. **fit run** squeezes the whole service into the window.

Tickets carry a picture of the dish, the larder is stocked with the
ingredients *this menu* needs, and each counter shows the tool of its trade -
all of it from `assets/sprites`, and all of it drawn over something the canvas
draws anyway, so a missing file costs a picture and nothing else. Hovering a
jar in the larder says which recipes want it and how many of them are in the
room at that moment - and when a cook is switching, it walks to *that* jar and
the shelf lights up, so the trip you are watching is the one its order needs.

**Hover anything.** A cook says what it is doing and which order it is on; a
ticket, an oven dish or a card in a place gives you the order's whole story -
when it arrived, how long the customer will stay, how much work is left, how
many times it has been dispatched and preempted, and its recipe; a station
says how many of its places are taken; a block on the timeline says which
order that cook was working on, which step, and for which ticks. On the
comparison table and the score card, hovering a metric shows the formula it is
worked out from.

Space pauses, the arrow keys step one tick, dragging on the timeline scrubs,
and clicking anything pins it in the inspector. The end-of-run card shows the
five components.

### The same commands on each platform

```bash
./start.sh play my-scheduler                         # one run, the numbers
./start.sh play my-scheduler --seed 7 --config rush  # a different day, a different profile
./start.sh play my-scheduler --replay                # record it and open the viewer
./start.sh evaluate my-scheduler --seeds 1000..1020  # mean and confidence interval
./start.sh compare my-scheduler                      # against every reference
./start.sh check my-scheduler                        # what the marker will say
./start.sh new second-try                            # scaffold another scheduler folder
./start.sh doctor                                    # what the tooling found
```

`play`, `evaluate` and `compare` also take the name of a reference scheduler
in place of a folder, so `./start.sh play sous_chef --replay` shows you a
reference working through the same day you just watched yours fail at. The
textbook name works just as well: `./start.sh play srtf --replay` is the same
run, and `baselines` prints what each one implements.

### Reading the output

```text
  my_scheduler on "lunch service"  seed 1234  cores 2  switch cost 2
  score 68.0   = completion 28.6 + response 6.1 + turnaround 6.5 + slowdown 3.9 + switching 15.0 + fairness 8.0

  orders                252    served 240 · abandoned 12 · unfinished 0
  completion            95.2%   priority-weighted 94.0%   abandoned by priority 1/2/3: 7/0/5
  response mean/p95     26.3 / 109 ticks   (10 never got a cook)
  turnaround mean/p95   55.8 / 204 ticks
  waiting mean/max      26.5 / 429 ticks
  slowdown mean/max     5.69 / 19.42   bounded 3.87 / 19.42
  context switches      369    369 necessary + 0 extra   (738 ticks, 21.6% of capacity)   preemptions 0
  utilisation           68.4%   idle 10.0%   wasted work 22 ticks
  station board         2 places   54.5% in use   full 25.8% of the service
  station pass          1 place   55.4% in use   full 55.4% of the service
  station bar           1 place   15.6% in use   full 15.6% of the service
  station stalls        0 assignments refused (a full station)   16 orders sent back to the rail
  fairness (Jain)       0.535
  decisions             618    mean 0.010 ms · slowest 0.140 ms · errors 0 · timeouts 0 · rejected 0
```

The second line is your mark, component by component - the six numbers add up
to the score. *Abandoned by priority* tells you who you are losing. *Station
stalls* is contention: refused assignments mean you asked for a cook at a full
station, and orders sent back to the rail mean a step finished and the station
its next step needed was busy. *Never got a cook* is the number of customers who
left without anybody starting their order. *Necessary + extra* is your
switching component in the raw. *Rejected* should be zero: if it is not, you
asked for something impossible - `check` says what.

### Seeds

A **seed** decides the day: which orders arrive, when, and how long each step
takes. The same seed is the same day every time, on every machine, so you can
replay exactly the situation you are debugging. Different seeds are different
days, and one day is not evidence: `evaluate` runs a set of them and reports
the mean with a 95% confidence interval. A difference smaller than the interval
is noise.

Seeds 1001 to 1005 are the public practice seeds that `check` uses. Marking uses
a hidden set.

---

## 9. Speed matters

`schedule` has **50 milliseconds** per call. A decision that takes longer is
discarded - the kitchen carries on as if you had said nothing - and counted as
a timeout. Twenty consecutive failures of any kind (a timeout, an exception, a
return value that is not a decision) **forfeit** the run: score 0.

That is a generous budget. The reference schedulers decide in microseconds and
a sensible Python scheduler in well under a millisecond. Where it bites is a
search over every ordering of a fifty-order rail. `check` reports your mean and
slowest decision.

---

## 10. What you hand in

**One file: `my-scheduler/scheduler.py`, uploaded to Moodle.** That is the
whole submission. Moodle knows who you are; there is nothing to fill in.

All of your code goes in that one file. A second `.py` file next to it is
**not importable** when the marker loads your scheduler - the file is loaded by
path, not as a package - so it is rejected rather than silently ignored. The
same goes for everything else: no `data/` folder, no notes, no README. Any
table your scheduler needs must be written out inside `scheduler.py`. The file
itself must stay under 256 KB.

Before you upload:

```bash
./start.sh check my-scheduler
```

runs exactly what the marker runs first: the folder's structure, then your
scheduler through every public seed under the marker's failure policy. A
submission that passes here is one the pipeline accepts.

---

## 11. Practice and marking

Your submission is run on every marking profile over a hidden seed set, with
the same engine, the same rules and the same failure policy you have here. Your
mark is the mean over profiles of your mean score per profile. The standings
page shows every run, so a bad day on one seed is visible and explained rather
than mysterious.

There is no sandbox. Your file is run as the marker's user, and the pipeline
flags - for a human to read - any import of `subprocess`, `socket`, the network
libraries, `pickle` and the like. A scheduler has no business making them.

---

## 12. Questions worth answering

None of these has its answer written down here, and the order they are in is
not the order of payoff - working that out is most of the exercise. Every one
of them can be settled in an afternoon with `compare`, a changed line and a
seed set wide enough to trust.

1. **Which orders are not worth starting?** `estimate_remaining(o)` and
   `o.time_left` between them can tell you that a customer will be gone before
   their dish is done. What is the right thing to do about that, and what does
   doing nothing about it cost you?
2. **What should the rail be sorted by?** `rank` in section 7 returns arrival
   order. There are at least six other things on `obs` and `order` you could
   return instead. Try them one at a time and keep the table.
3. **When is taking a cook off a dish worth it?** Preempting is allowed and
   costs `obs.kitchen.switch_cost` twice - once to switch away, once to resume.
   So there is a threshold, and it is not zero. Find roughly where it is, and
   check your answer against the references: one of them preempts eagerly, one
   hardly at all, and the table says which strategy this kitchen rewards.
4. **Use the oven.** `work_until_wait` says how soon an order will free its cook
   by going into the oven. A burger's first step is three ticks and then
   twenty in the grill; starting it is almost free, and it will need a cook
   again later - plan for that.
5. **Set an alarm.** `wake_in(ticks)` is your timer interrupt: it asks the
   kitchen to call you again when nothing has happened but time has passed.
   On most profiles it earns nothing - they are so busy that an event always
   beats the timer to it, and waking on every single tick of the course
   profile changes a score by two hundredths of a point. On `function` it is
   the whole game. Work out which kind of kitchen you are in before you spend
   effort here, and know that `obs.reason.kinds` tells you when a timer is
   what woke you.
6. **Watch the deadline, not just the length.** Two jobs the same length: serve
   the customer with less `time_left`. Set an alarm with `wake_in` so that a
   customer who is about to give up is looked at even when nothing else is
   happening.
7. **Priorities.** VIPs (priority 3) have the least patience on the menu and
   `abandoned by priority` shows who you are losing. The score does not weight
   priority, but a VIP abandoned is still an order abandoned - and the
   `priority_completion` figure is on the standings page.
8. **Estimate better in the dark.** On `blind`, `estimate_remaining` is a mean
   over history. The steps already finished tell you which *kind* of day this
   order is having; the recipe's shape tells you how many work steps remain.
9. **Balance the brigade.** With four cooks, moving an order between cooks pays
   a switch for nothing. Keep an order where it is unless there is a reason.
10. **Feed the bottleneck.** Find the station with the least slack - the one
   `obs.stations` keeps showing as full, and the result page reports as
   saturated - and never let it stand empty. Everything else in the kitchen
   runs at the rate that one station clears, so a place there left idle is
   throughput you do not get back. On `service-line` this is the whole game.
11. **Do not walk into a full station.** When a work step ends and the next one
    is at a different station that is full, the order is bumped back to the
    rail and somebody pays a switch to resume it. You can see that coming:
    `order.steps[order.step + 1].station` says where it is going next, and
    `obs.stations` says whether there will be room. Preferring an order that
    will not bump is worth real points on tight profiles.

---

## 13. When something goes wrong

**`ModuleNotFoundError: No module named 'kitchen'`** - you ran `python
scheduler.py` directly. Run it through `./start.sh play my-scheduler`, which
puts the SDK on the path.

**`no Scheduler subclass found`** - your class does not inherit from
`Scheduler`, or the file does not define one. `class MyScheduler(Scheduler):`.

**`defines several schedulers`** - two subclasses in one file. Delete one, or
name it: `./start.sh play my-scheduler/scheduler.py:MyScheduler`.

**`rejected assignments`** - you assigned an order that was in the oven,
already served, not yet arrived, or already on another cook, or you named a cook
twice. Check `order.is_ready` before assigning, and use `obs.ready`.

**Everybody walks out** - does your scheduler ever assign anybody? A
`Decision()` with nothing in it is legal and does exactly that. `fill_idle` is
the smallest fix.

**Score 0 and `forfeit`** - twenty consecutive failed decisions. `check` shows
the first few messages; almost always an exception on a path you did not test,
such as an order with no steps left or an empty rail.

**`over deadline`** - a decision took more than 50 ms. Look for anything that
grows with the size of the rail squared, and for `time.sleep`.

**The viewer shows nothing** - the run was recorded but the page did not open.
The dashboard's **Recent replays** lists it; or open the viewer and pick it.

**Different numbers on a friend's machine** - same seed, same profile, same
engine version should be identical to the tick. Check the `engine` and `config`
hashes printed at the bottom of every run: a different profile file hashes
differently, and that is the first thing to compare.
