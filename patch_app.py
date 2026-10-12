import codecs

with codecs.open("app.py", "r", "utf-8") as f:
    content = f.read()

start_str = "    def _race_sim_work(self, worker=None):"
end_str = "    # ═══════════════  APPLY SEED  ═══════════════════════════════"

idx1 = content.find(start_str)
idx2 = content.find(end_str)

new_code = """    def _race_sim_work(self, worker=None):
        from race_simulator import run_strategy_evolution
        from hull_matcher import match_hull
        import canoe_physics

        canoe_physics.PADDLER_POWER_W = float(self.power_var.text())

        course_names, course_weights = [], []
        if self.race_sprint_var.isChecked():
            course_names.append("sprint")
            course_weights.append(float(self.wt_sprint_var.text()))
        if self.race_slalom_var.isChecked():
            course_names.append("slalom")
            course_weights.append(float(self.wt_slalom_var.text()))
        if self.race_endure_var.isChecked():
            course_names.append("endurance")
            course_weights.append(float(self.wt_endure_var.text()))

        if not course_names:
            print("❌ Select at least one course!")
            return None

        n_gen = int(self.evo_gen_var.text())

        def ui_cb(gen, best_time, best_genome):
            frac = min(gen / max(n_gen, 1), 1.0) * 0.5
            if worker:
                worker.progress_signal.emit(frac, f"Tier 1 Gen {gen}/{n_gen}  Best: {best_time:.1f}s")

        results = run_strategy_evolution(
            course_names=course_names,
            course_weights=course_weights,
            pop_size=int(self.evo_pop_var.text()),
            n_generations=n_gen,
            ui_callback=ui_cb,
            stop_event=self._stop,
        )
        
        if self._stop.is_set(): return results
        
        if worker:
            worker.progress_signal.emit(0.6, "Tier 2: Physical Hull Matching...")
            
        ideal_Cs = results["ideal_Cs"]
        ideal_Ct = results["ideal_Ct"]
        hull_length = float(self.evo_hull_len.text())
        mass_target = 120.0 + 4 * canoe_physics.PADDLER_POWER_W  # Approximation for paddler mass is 75, wait, just hardcode 120 + 4*75
        mass_target = 420.0
        
        match_results = match_hull(ideal_Cs, ideal_Ct, L=hull_length, mass_target=mass_target)
        
        results["best_hull"] = match_results
        
        self._race_results = results
        
        if worker:
            worker.progress_signal.emit(1.0, "Matching Complete!")
            
        return results

    def _on_race_sim_finished(self, results):
        if not results:
            return
            
        print("[AppUI] _on_race_sim_finished SLOT TRIGGERED ON MAIN THREAD")
        self._set_progress(1.0, "Race Sim Complete")
        self._display_race_results(results)
        
        hull_length = float(self.evo_hull_len.text())
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(500, lambda: self._prompt_apply_seed(results, hull_length))

    def _display_race_results(self, results):
        self._weights_text.clear()

        lines = []
        lines.append("═" * 60)
        lines.append("  📊  NESTED OPTIMIZATION RESULTS")
        lines.append("═" * 60)
        lines.append("")
        lines.append("  TIER 1: ABSTRACT STRATEGY")
        lines.append("  " + "─" * 55)
        lines.append(f"    Ideal Straight Capability (Cs): {results['ideal_Cs']:.3f}")
        lines.append(f"    Ideal Turning Capability (Ct):  {results['ideal_Ct']:.3f}")
        lines.append(f"    Abstract Race Time:             {results['baseline_time']:.1f} s")
        lines.append("")
        lines.append("  TIER 2: PHYSICAL HULL MATCH")
        lines.append("  " + "─" * 55)
        hull = results["best_hull"]
        lines.append(f"    Beam:    {hull['beam']:.3f} m")
        lines.append(f"    Draft:   {hull['draft']:.3f} m")
        lines.append(f"    Factors: {[round(f, 2) for f in hull['factors']]}")
        lines.append(f"    Match Error: {hull['error']:.5f}")

        self._weights_text.setText("\\n".join(lines))
        self._plot_weight_chart(results)
        
        # Load telemetry into replay widget and switch to it
        print(f"[AppUI] _display_race_results checking for telemetry. Keys in results: {list(results.keys())}")
        if "telemetry" in results:
            print(f"[AppUI] Telemetry found. Passing to load_telemetry.")
            try:
                self.replay_widget.load_telemetry(results["telemetry"])
                self.result_tabs.setCurrentWidget(self._replay_tab)
                print(f"[AppUI] Successfully switched to Replay tab.")
            except Exception as e:
                import traceback
                print(f"[AppUI] ERROR while loading telemetry: {e}")
                traceback.print_exc()
        else:
            print(f"[AppUI] WARNING: 'telemetry' key NOT found in results!")

    def _plot_weight_chart(self, results):
        pass

"""

if idx1 != -1 and idx2 != -1:
    new_content = content[:idx1] + new_code + content[idx2:]
    with codecs.open("app.py", "w", "utf-8") as f:
        f.write(new_content)
    print("Success")
else:
    print("Indices not found", idx1, idx2)
