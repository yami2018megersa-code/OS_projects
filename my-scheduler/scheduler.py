"""Your scheduler. Edit this file.

Everything you write goes in here. A submission is one file of code - extra
.py files next to it are not importable when the server loads your scheduler,
so they are rejected rather than silently ignored.
"""

from kitchen import Decision, Scheduler


class MyScheduler(Scheduler):
    name = "my_scheduler"
    version = "2"

    def reset(self, seed):
        """Called once before each run. Set up any per-run state here."""
        # Per-recipe EMA of total work, used on blind profiles for better
        # estimation than the engine's crude mean.
        self._recipe_ema = {}           # recipe_name -> smoothed total_work
        self._ema_alpha = 0.3           # smoothing factor
        self._seen_history_ids = set()  # track processed history entries

    # ---- estimation --------------------------------------------------------

    def _update_ema(self, obs):
        """Update per-recipe EMA from newly served orders only.

        obs.history is a sliding window of the last 60 served orders. We track
        which order ids we've already processed to avoid double-counting, which
        would over-weight old data and skew the estimate.
        """
        for h in obs.history:
            if h.id not in self._seen_history_ids:
                self._seen_history_ids.add(h.id)
                name = h.recipe
                tw = h.total_work
                if name in self._recipe_ema:
                    self._recipe_ema[name] = (
                        self._ema_alpha * tw
                        + (1.0 - self._ema_alpha) * self._recipe_ema[name]
                    )
                else:
                    self._recipe_ema[name] = float(tw)

    def _estimate(self, obs, order):
        """Work remaining: true value when known, EMA on blind profiles."""
        if order.work_remaining is not None:
            return float(order.work_remaining)
        if order.recipe in self._recipe_ema:
            return max(1.0, self._recipe_ema[order.recipe] - order.work_done)
        return obs.estimate_remaining(order)

    # ---- feasibility -------------------------------------------------------

    def _is_doomed(self, obs, order):
        """True if the order cannot finish before its deadline.

        Accounts for whether the order is already running (no switch needed)
        or still on the rail (needs a full switch_cost).
        """
        est = self._estimate(obs, order)
        sc = obs.kitchen.switch_cost
        # Already running — check if switch is still being paid
        if order.is_running and order.core is not None:
            core = obs.core(order.core)
            if core is not None:
                if core.is_working:
                    return est > order.time_left
                if core.is_switching:
                    return est + core.switch_remaining > order.time_left
        # On the rail — needs at least one switch to start
        return est + sc > order.time_left

    # ---- ranking -----------------------------------------------------------

    def _rank(self, obs, order):
        est = self._estimate(obs, order)
        sc = obs.kitchen.switch_cost
        slack = order.time_left - est - sc
        
        effective_slack = slack - (order.priority - 1) * 50
        effective_slack = max(effective_slack, 0)
        
        wuw = order.work_until_wait
        if wuw is None:
            wuw = est
            
        wr = order.work_remaining
        if wr is None:
            wr = est
            
        # True SJF: Prioritize by total work remaining, square it to aggressively prefer short jobs
        base = (wr ** 2) * 100 + wuw
        
        # Prioritize started jobs to reduce turnaround time and slowdown
        if order.has_started:
            base *= 0.5
            
        score = base
        
        # Tie-breaker is slack
        score += effective_slack * 0.001
        
        # Prevent starvation
        age = obs.time - order.arrival
        score -= age * 20
        
        # Penalize orders that will bump, and boost orders that feed free ovens
        idx = order.step + 1
        if idx < len(order.steps):
            nxt = order.steps[idx]
            
            if nxt.kind == "work" and nxt.station is not None:
                stn_obj = obs.station(nxt.station)
                if stn_obj is not None and stn_obj.is_full:
                    score += 500000
                    
            elif nxt.kind == "wait" and nxt.station is not None:
                stn_obj = obs.station(nxt.station)
                if stn_obj is not None and not stn_obj.is_full:
                    # The sooner it reaches the free oven, the bigger the priority boost
                    boost = max(0, 100 - wuw) * 1000
                    score -= boost
                    
        return score


    # ---- profile detection -------------------------------------------------

    def _is_sparse(self, obs):
        """True if no recipe on the menu has wait/oven steps.

        Identifies function-like profiles where events are rare and wake_in
        with round-robin is essential for response time and fairness.
        """
        for r in obs.recipes.values():
            if any(kind == "wait" for _, kind, _ in r.steps):
                return False
        return True

    # ---- schedule ----------------------------------------------------------

    def schedule(self, obs):
        """Called at every scheduling point. Decide who cooks what."""
        self._update_ema(obs)

        decision = Decision()
        sc = obs.kitchen.switch_cost
        sparse = self._is_sparse(obs)
        rr_quantum = max(sc * 4, 8)  # round-robin quantum for sparse profiles

        # ===== TRIAGE: separate viable from doomed =====
        viable, doomed = [], []
        for o in obs.ready:
            (doomed if self._is_doomed(obs, o) else viable).append(o)

        # Sort viable by composite rank; doomed go at the end as fallback
        viable.sort(key=lambda o: self._rank(obs, o))
        rail = viable + doomed

        # ===== ASSIGN IDLE COOKS =====
        free = obs.free_stations()
        a_orders = set()   # order ids assigned in this decision
        a_cores = set()    # core ids reassigned in this decision

        for core in obs.idle_cores:
            for order in rail:
                if order.id in a_orders:
                    continue
                st = order.station
                if st is not None and free.get(st, 0) <= 0:
                    continue
                # Only skip doomed if viable alternatives exist
                if order in doomed and viable:
                    continue
                decision.assign(core, order)
                a_orders.add(order.id)
                if st is not None:
                    free[st] -= 1
                break

        # Viable orders not yet assigned — candidates for preemption
        rest = [o for o in viable if o.id not in a_orders]

        # ===== PREEMPTION PHASE 1: doomed running orders =====
        for core in obs.working_cores:
            cur = obs.order_on(core)
            if cur is not None and self._is_doomed(obs, cur):
                # Preempt it if there is ANY viable candidate, or just to free the cook
                if rest:
                    cand = rest[0]
                    decision.assign(core, cand)
                    a_cores.add(core.id)
                    a_orders.add(cand.id)
                    rest.remove(cand)
                    if cand.station is not None:
                        free[cand.station] = free.get(cand.station, 0) - 1
                    if cur.station is not None:
                        free[cur.station] = free.get(cur.station, 0) + 1

        # ===== PREEMPTION PHASE 2: Mathematically Sound SRTF =====
        for core in obs.working_cores:
            if core.id in a_cores or not rest:
                continue
            if core.is_switching:
                continue
                
            cur = obs.order_on(core)
            if cur is None:
                continue
                
            cur_wuw = cur.work_until_wait
            if cur_wuw is None:
                cur_wuw = self._estimate(obs, cur)
                
            # Find the best candidate to preempt with
            best_cand = min(rest, key=lambda o: o.work_until_wait or self._estimate(obs, o))
            cand_wuw = best_cand.work_until_wait or self._estimate(obs, best_cand)
            
            # Mathematical condition for preemption to improve average turnaround:
            # Only preempt for extremely short jobs to preserve necessary switch ratio
            if cand_wuw <= 5 and cand_wuw + 8 * sc < cur_wuw:
                # Make sure we don't cause cur to miss its deadline
                cur_est = self._estimate(obs, cur)
                if sc + cand_wuw + sc + cur_est <= cur.time_left:
                    # Make sure the candidate's station is free
                    st = best_cand.station
                    cur_st_bonus = 1 if cur.station == st else 0
                    if st is None or (free.get(st, 0) + cur_st_bonus) > 0:
                        decision.assign(core, best_cand)
                        a_cores.add(core.id)
                        a_orders.add(best_cand.id)
                        rest.remove(best_cand)
                        if st is not None:
                            free[st] = free.get(st, 0) - 1
                        if cur.station is not None:
                            free[cur.station] = free.get(cur.station, 0) + 1

        # ===== ROUND-ROBIN ON SPARSE PROFILES =====
        # On function-like profiles (no oven, long jobs, sparse events),
        # response time (30% weight) and fairness (25% weight) dominate.
        # Round-robin via wake_in ensures every order gets attention quickly.
        if sparse and obs.working_cores:
            unassigned = [o for o in obs.ready if o.id not in a_orders]
            if unassigned:
                # Preempt ALL cooks that have exceeded the quantum
                for core in sorted(obs.working_cores,
                                   key=lambda c: -c.running_for):
                    if core.id in a_cores:
                        continue
                    if core.running_for < rr_quantum:
                        break  # sorted desc — no further cores exceed quantum
                    if not unassigned:
                        break
                    cur = obs.order_on(core)
                    for o in unassigned:
                        st = o.station
                        cur_st_bonus = 1 if cur is not None and cur.station == st else 0
                        if st is not None and (free.get(st, 0) + cur_st_bonus) <= 0:
                            continue
                        if cur is not None and o.id != cur.id:
                            decision.assign(core, o)
                            a_cores.add(core.id)
                            a_orders.add(o.id)
                            unassigned.remove(o)
                            if o.station is not None:
                                free[o.station] = free.get(o.station, 0) - 1
                            if cur.station is not None:
                                free[cur.station] = free.get(
                                    cur.station, 0) + 1
                            break

        # ===== TIMER =====
        # Compute the minimum useful wake time from all sources.
        wake = None

        # Source 1: sparse quantum timer (round-robin)
        if sparse:
            if obs.working_cores:
                max_run = max(c.running_for for c in obs.working_cores)
                sparse_wake = max(1, rr_quantum - max_run)
            elif obs.busy_cores:
                # All busy cooks are switching — wake after switch + quantum
                max_sw = max(c.switch_remaining for c in obs.busy_cores)
                sparse_wake = max(1, max_sw + rr_quantum)
            else:
                sparse_wake = rr_quantum
            wake = sparse_wake

        # Source 2: deadline timer — wake before the soonest expiry
        soonest = None
        for o in obs.orders:
            if not o.is_blocked:
                if soonest is None or o.time_left < soonest:
                    soonest = o.time_left
        if soonest is not None and soonest > 1:
            deadline_wake = max(1, soonest - sc - 2)
            wake = deadline_wake if wake is None else min(wake, deadline_wake)

        # Source 3: quantum timer for non-sparse with waiting viable orders
        if not sparse and rest and obs.busy_cores:
            q = max(5, sc * 3)
            wake = q if wake is None else min(wake, q)

        if wake is not None:
            decision.wake_in(wake)

        # ===== ANNOTATE =====
        return decision.annotate(
            text=f"t={obs.time} v={len(viable)} d={len(doomed)} "
                 f"i={len(obs.idle_cores)} b={len(obs.busy_cores)}",
            queue=[o.id for o in rail],
        )
