"""Your scheduler. Edit this file.

Everything you write goes in here. A submission is one file of code - extra
.py files next to it are not importable when the server loads your scheduler,
so they are rejected rather than silently ignored.
"""

from kitchen import Decision, Scheduler


class MyScheduler(Scheduler):
    name = "my_scheduler"
    version = "1"

    def reset(self, seed):
        """Called once before each run. Set up any per-run state here."""
        # Per-recipe EMA of total work, used on blind profiles for better
        # estimation than the engine's crude mean.
        self._recipe_ema = {}   # recipe_name -> smoothed total_work
        self._ema_alpha = 0.3   # smoothing factor

    # ---- estimation --------------------------------------------------------

    def _estimate(self, obs, order):
        """Work remaining estimate, preferring our EMA on blind profiles."""
        if order.work_remaining is not None:
            return float(order.work_remaining)
        # Try our EMA first (better than the engine's flat mean on blind)
        if order.recipe in self._recipe_ema:
            est = self._recipe_ema[order.recipe] - order.work_done
            return max(1.0, est)
        # Fall back to the engine's estimate
        return obs.estimate_remaining(order)

    def _update_ema(self, obs):
        """Update per-recipe EMA from recently served orders."""
        for h in obs.history:
            name = h.recipe
            tw = h.total_work
            if name in self._recipe_ema:
                self._recipe_ema[name] = (
                    self._ema_alpha * tw
                    + (1.0 - self._ema_alpha) * self._recipe_ema[name]
                )
            else:
                self._recipe_ema[name] = float(tw)

    # ---- feasibility -------------------------------------------------------

    def _is_doomed(self, obs, order):
        """True if the order cannot possibly be served before its deadline."""
        est = self._estimate(obs, order)
        # If it's already working, there is no switch cost to continue
        is_working = any(obs.order_on(c) == order for c in obs.working_cores)
        cost = 0 if is_working else obs.kitchen.switch_cost
        return est + cost > order.time_left

    # ---- scoring -----------------------------------------------------------

    def _rank(self, obs, order):
        """Lower score = higher priority. Composite key balancing multiple
        objectives:
          - Shortest estimated remaining time (SRTF core)
          - Deadline urgency via slack
          - Oven proximity (orders about to enter oven free the cook quickly)
          - Priority boost for VIPs
        """
        est = self._estimate(obs, order)
        sc = obs.kitchen.switch_cost
        slack = order.time_left - est - sc

        # --- component 1: SRTF base (normalized) ---
        srtf = est

        # --- component 2: deadline urgency ---
        # Slack is time left after working. Small slack = extremely urgent.
        # Since we want urgent orders to have a lower score, we just use slack.
        urgency = slack

        # --- component 3: oven proximity ---
        # Orders close to an oven step free the cook quickly: prefer them.
        wuw = order.work_until_wait
        if wuw is None:
            wuw = est  # blind: assume no oven benefit
        oven_bonus = wuw  # lower wuw = sooner the cook is free = better

        # --- component 4: priority ---
        # VIP (3) gets divided more, lowering score => higher priority
        priority_divisor = order.priority

        # --- component 5: starvation (age) ---
        # Forces rotation. Age is time since arrival.
        age = obs.time - order.arrived_at
        if order.work_done == 0:
            starvation = age * 0.8  # untouched: priority grows fast to minimize response time
        else:
            starvation = age * 0.2  # touched: priority grows slow to ensure eventual fairness

        # Composite: primarily SRTF, with urgency and oven tiebreakers, minus starvation
        score = (0.50 * srtf + 0.30 * oven_bonus + 0.25 * urgency - starvation) / priority_divisor
        return score

    # ---- next-station lookahead -------------------------------------------

    def _will_bump(self, obs, order, free):
        """Check if the order's NEXT step after the current one is at a station
        that is currently full, meaning it will get bumped back to the rail."""
        idx = order.step + 1
        if idx >= len(order.steps):
            return False  # no next step
        nxt = order.steps[idx]
        if nxt.kind == "wait":
            return False  # oven needs no station
        if nxt.station is None:
            return False
        return free.get(nxt.station, 0) <= 0

    # ---- main scheduling ---------------------------------------------------

    def schedule(self, obs):
        """Called at every scheduling point. Decide who cooks what."""
        self._update_ema(obs)

        decision = Decision()
        sc = obs.kitchen.switch_cost

        # ----- build the sorted rail -----
        # Separate doomed orders from viable ones
        viable = []
        doomed = []
        for o in obs.ready:
            if self._is_doomed(obs, o):
                doomed.append(o)
            else:
                viable.append(o)

        # Sort viable orders: lowest rank score = highest priority
        viable.sort(key=lambda o: self._rank(obs, o))
        # Doomed orders go at the end (we won't assign them unless nothing
        # else is available, to avoid wasting cooks)
        rail = viable + doomed

        # ----- assign idle cooks -----
        free = obs.free_stations()
        assigned_in_decision = set()

        for core in obs.idle_cores:
            for i, order in enumerate(rail):
                if order.id in assigned_in_decision:
                    continue
                # Check station capacity
                station = order.station
                if station is not None and free.get(station, 0) <= 0:
                    continue
                # Prefer orders that won't bump on the next step
                # (but don't skip them entirely — just deprioritize via rank)
                # Skip doomed orders if viable ones exist
                if order in doomed and viable:
                    continue
                decision.assign(core, order)
                assigned_in_decision.add(order.id)
                if station is not None:
                    free[station] -= 1
                break

        # ----- conservative preemption -----
        # Only preempt if a viable ready order is significantly better than
        # what a cook is currently working on, and the cost is justified.
        rest = [o for o in rail
                if o.id not in assigned_in_decision and o not in doomed]

        for core in obs.working_cores:
            if not rest:
                break
            current = obs.order_on(core)
            if current is None:
                continue

            # Don't preempt if the current order is almost done
            cur_est = self._estimate(obs, current)
            if cur_est <= sc * 2:
                continue

            candidate = None
            for o in rest:
                station = o.station
                if station is not None and free.get(station, 0) <= 0:
                    continue
                candidate = o
                break

            if candidate is None:
                continue

            cand_score = self._rank(obs, candidate)
            curr_score = self._rank(obs, current)

            # Only preempt if the candidate is materially better.
            # The threshold accounts for the double switch cost.
            threshold = sc * 2.5
            if curr_score - cand_score > threshold:
                decision.assign(core, candidate)
                rest.remove(candidate)
                if candidate.station is not None:
                    free[candidate.station] = free.get(candidate.station, 0) - 1

        # ----- preempt cooks working on doomed orders -----
        # If a cook is working on an order that has become doomed and there
        # are viable orders waiting, free the cook.
        for core in obs.busy_cores:
            current = obs.order_on(core)
            if current is None:
                continue
            if not self._is_doomed(obs, current):
                continue
            # Only preempt if there's a viable order waiting that can start
            viable_waiting = [o for o in rest
                              if o.station is None
                              or free.get(o.station, 0) > 0]
            if viable_waiting:
                best = viable_waiting[0]
                decision.assign(core, best)
                if best in rest:
                    rest.remove(best)
                if best.station is not None:
                    free[best.station] = free.get(best.station, 0) - 1
                # Give back the station the preempted order held
                if current.station is not None:
                    free[current.station] = free.get(current.station, 0) + 1

        # ----- set a wake-up timer -----
        # Wake up before the next order expires so we can react in time.
        # Also useful on sparse profiles like 'function'.
        soonest_expiry = None
        for o in obs.orders:
            if o.is_blocked:
                continue
            if soonest_expiry is None or o.time_left < soonest_expiry:
                soonest_expiry = o.time_left

        if soonest_expiry is not None and soonest_expiry > 1:
            # Wake up a few ticks before the deadline
            wake_tick = max(1, soonest_expiry - sc - 1)
            decision.wake_in(wake_tick)

        # Set a periodic timer to rotate cooks if there are viable orders waiting,
        # or if we are in a sparse profile waiting for events.
        waiting_viable = [o for o in rest if o not in doomed]
        if waiting_viable and obs.busy_cores:
            quantum = max(5, sc * 3)
            if soonest_expiry is None or quantum < soonest_expiry - sc - 1:
                decision.wake_in(quantum)
        elif not obs.ready and not obs.blocked and obs.busy_cores:
            quantum = max(10, sc * 5)
            if soonest_expiry is None or quantum < soonest_expiry - sc - 1:
                decision.wake_in(quantum)

        return decision.annotate(
            text=f"t={obs.time} | {len(viable)} viable, {len(doomed)} doomed | "
                 f"idle={len(obs.idle_cores)} busy={len(obs.busy_cores)}",
            queue=[o.id for o in rail],
        )
