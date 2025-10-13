"""
Copyright (c) 2025, Rick Lan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, and/or sublicense,
for non-commercial purposes only, subject to the following conditions:

- The above copyright notice and this permission notice shall be included in
  all copies or substantial portions of the Software.
- Commercial use (e.g. use in a product, service, or activity intended to
  generate revenue) is prohibited without explicit written permission from
  the copyright holder.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_MDL
import numpy as np
from cereal import log

class AEM:
    # --- Configuration Constants ---
    # Speed thresholds (m/s)
    SPEED_THRESHOLD_HIGHWAY = 22.23  # m/s (approx. 80 kph)
    SPEED_THRESHOLD_CITY = 15.27     # m/s (approx. 55 kph)
    SPEED_THRESHOLD_LOW = 5.56       # m/s (approx. 20 kph)
    SPEED_THRESHOLD_CREEP = 2.23     # m/s (approx. 8 kph)

    # Lead related thresholds
    LEAD_TTC_CRITICAL = 1.75        # seconds, time to collision
    LEAD_TTC_CAUTION = 3.0
    LEAD_DIST_VERY_CLOSE = 10.0    # meters
    LEAD_DIST_FAR_HIGHWAY = 85.0   # meters, for considering lead far enough on highway
    LEAD_DIST_DEFAULT_NO_LEAD = 150.0 # Default distance for EMA when no lead

    LEAD_ACCEL_HARD_BRAKE = -3.0   # m/s^2
    LEAD_ACCEL_MILD_BRAKE = -2.0   # m/s^2
    LEAD_ACCEL_PULLING_AWAY = 0.5  # m/s^2

    # Steering thresholds
    STEERING_ANGLE_ABS_HIGH_CURVATURE = 45.0  # degrees (EMA value)

    # Hysteresis & Timers
    HYSTERESIS_FRAMES_TO_SWITCH = 10  # Approx 0.5s at 20Hz
    LEAD_LOST_FRAMES_TO_FALLBACK_BASE = 40 # Approx 2s at 20Hz

    # EMA filter time constants (seconds) - THESE ARE DESIGN PARAMETERS
    EMA_TC_V_EGO = 1.0
    EMA_TC_LEAD_DREL = 0.5
    EMA_TC_LEAD_V_ABS = 0.5 # Filters absolute lead speed
    EMA_TC_LEAD_ALEAD = 0.5
    EMA_TC_STEERING_ANGLE_ABS = 0.8
    EMA_TC_V_MODEL_ERROR = 1.0

    # Model & Planner Related Thresholds
    MODEL_VEL_ERROR_THRESHOLD = 2.0 # m/s (EMA value)
    MIN_VISION_LEAD_PROB_ACTION = 0.5 # Min modelProb for acting on vision-only leads

    # Other
    REL_SPEED_SIGNIFICANT_DIFFERENCE = 2.5 # m/s

    def __init__(self, debug=False):
        self.enabled = False
        self._current_mode = 'acc' # Default to ACC
        self._target_mode_suggestion = None
        self._mode_switch_counter = 0
        self.debug = debug
        self._lead_id_prev = -1
        self._lead_absence_frames = 0
        self.personality = log.LongitudinalPersonality.standard

        # --- Calculate alpha values from time constants (tau) ---
        def get_alpha(tau, dt):
            """Calculates EMA alpha from time constant and time step."""
            # Ensure tau > 0 to avoid division by zero if dt=0 somehow
            # Also handle potential case where dt is zero
            return dt / (tau + dt) if tau > 1e-5 and dt > 1e-5 else 1.0 # Default to alpha=1 (no filtering) if tau or dt is invalid/zero

        alpha_v_ego = get_alpha(AEM.EMA_TC_V_EGO, DT_MDL)
        alpha_lead_drel = get_alpha(AEM.EMA_TC_LEAD_DREL, DT_MDL)
        alpha_lead_v_abs = get_alpha(AEM.EMA_TC_LEAD_V_ABS, DT_MDL)
        alpha_lead_alead = get_alpha(AEM.EMA_TC_LEAD_ALEAD, DT_MDL)
        alpha_steering_angle_abs = get_alpha(AEM.EMA_TC_STEERING_ANGLE_ABS, DT_MDL)
        alpha_v_model_error = get_alpha(AEM.EMA_TC_V_MODEL_ERROR, DT_MDL)

        # --- Initialize EMA Filters using alpha ---
        # Ensure FirstOrderFilter takes 'alpha' as keyword arg
        try:
            self._v_ego_ema = FirstOrderFilter(0.0, alpha=alpha_v_ego, dt=DT_MDL)
            self._lead_drel_ema = FirstOrderFilter(AEM.LEAD_DIST_DEFAULT_NO_LEAD, alpha=alpha_lead_drel, dt=DT_MDL)
            self._lead_v_ema = FirstOrderFilter(0.0, alpha=alpha_lead_v_abs, dt=DT_MDL) # Filters absolute V_lead
            self._lead_alead_ema = FirstOrderFilter(0.0, alpha=alpha_lead_alead, dt=DT_MDL)
            self._steering_angle_abs_ema = FirstOrderFilter(0.0, alpha=alpha_steering_angle_abs, dt=DT_MDL)
            # self._v_model_error_ema = FirstOrderFilter(0.0, alpha=alpha_v_model_error, dt=DT_MDL)
        except TypeError:
            # Fallback if the specific FirstOrderFilter expects positional args differently
            print("Warning: FirstOrderFilter init failed with keyword 'alpha', attempting positional.")
            self._v_ego_ema = FirstOrderFilter(0.0, alpha_v_ego, DT_MDL)
            self._lead_drel_ema = FirstOrderFilter(AEM.LEAD_DIST_DEFAULT_NO_LEAD, alpha_lead_drel, DT_MDL)
            self._lead_v_ema = FirstOrderFilter(0.0, alpha_lead_v_abs, DT_MDL)
            self._lead_alead_ema = FirstOrderFilter(0.0, alpha_lead_alead, DT_MDL)
            self._steering_angle_abs_ema = FirstOrderFilter(0.0, alpha_steering_angle_abs, DT_MDL)
            # self._v_model_error_ema = FirstOrderFilter(0.0, alpha_v_model_error, DT_MDL)


        self._log(f"AEM Initialized. Alphas: v_ego={alpha_v_ego:.3f}, dRel={alpha_lead_drel:.3f}, vLead={alpha_lead_v_abs:.3f}, aLead={alpha_lead_alead:.3f}, steer={alpha_steering_angle_abs:.3f}, v_err={alpha_v_model_error:.3f}")

    def _log(self, msg: str):
        """Logs debug messages if debug is enabled."""
        if self.debug:
            # Consider using cloudlog for persistent logs during testing
            # from openpilot.common.swaglog import cloudlog
            # cloudlog.debug(f"[AEM]: {msg}")
            print(f"[AEM]: {msg}")

    def _calculate_ttc(self, dist: float, ego_speed: float, lead_speed: float) -> float:
        """Calculates Time To Collision (TTC), returns infinity if collision is not imminent."""
        relative_speed = ego_speed - lead_speed
        if dist > 0.1 and relative_speed > 0.3: # Thresholds to avoid noise and division issues
            # Avoid division by zero or very small numbers
            return max(0.0, dist / relative_speed) # Return non-negative TTC
        return float('inf') # Return infinity if no collision course or relative speed is too low

    def _reset_lead_emas(self, d_lead_raw, v_lead_raw, a_lead_raw):
        """Helper to reset/initialize lead EMAs' state."""
        # This assumes direct state access/setting via '.x' is the correct method
        # for the FirstOrderFilter implementation being used.
        try:
            self._lead_drel_ema.x = float(d_lead_raw)
            self._lead_v_ema.x = float(v_lead_raw)
            self._lead_alead_ema.x = float(a_lead_raw)
            # Ensure filter knows it's 'initialized' if it uses that flag internally
            self._lead_drel_ema.initialized = True
            self._lead_v_ema.initialized = True
            self._lead_alead_ema.initialized = True
            self._log(f"Reset lead EMAs with raw values: d={d_lead_raw:.2f}, v={v_lead_raw:.2f}, a={a_lead_raw:.2f}")
        except AttributeError:
            self._log("Warning: Could not directly set '.x' on FirstOrderFilter. Reset may not be immediate.")
            # If direct access fails, update might be the only way, causing a slight delay in reset
            self._lead_drel_ema.update(d_lead_raw)
            self._lead_v_ema.update(v_lead_raw)
            self._lead_alead_ema.update(a_lead_raw)

    def set_personality(self, v_ego, personality):
        self.personality = personality
        if self.enabled:
            self.personality = log.LongitudinalPersonality.aggressive if v_ego > 16.67 else self.personality
        return self.personality

    # Note: Removed 'model_predicted_max_curvature_upcoming_raw' from signature
    def get_mode(self, v_ego_raw: float, lead_one_data_raw, steering_angle_deg_raw: float, standstill_raw: bool,
                 long_personality: int, allow_throttle_planner: bool,
                 model_path_plan_raw: dict, a_target_from_prev_cycle: float, model_predicts_stop_prev: bool,
                 fcw_active_prev: bool) -> str:
        """
        Determines the appropriate MPC mode ('acc' or 'blended') based on current conditions.
        ACC is primary, Blended is assist.
        """
        if not self.enabled:
            # Should not be called if not enabled, but return primary mode as safeguard
            return 'acc'

        # 0. Initialize suggested mode with AEM's current internal state
        suggested_mode = self._current_mode

        # 1. Update common EMA filters
        self._v_ego_ema.update(v_ego_raw)
        self._steering_angle_abs_ema.update(abs(steering_angle_deg_raw))
        # Use absolute value of model error for thresholding
        # self._v_model_error_ema.update(abs(v_model_error_raw))

        # 2. Process Lead Data & Update Lead EMAs
        lead_status = lead_one_data_raw.status
        current_lead_id = lead_one_data_raw.radarTrackId if lead_status else -1
        d_lead_raw = lead_one_data_raw.dRel if lead_status else AEM.LEAD_DIST_DEFAULT_NO_LEAD
        v_lead_raw = lead_one_data_raw.vLead if lead_status else self._v_ego_ema.x # Default to filtered ego if no lead
        a_lead_raw = lead_one_data_raw.aLeadK if lead_status else 0.0
        model_prob_lead = lead_one_data_raw.modelProb if lead_status else 0.0

        if lead_status:
            # Reset EMA if lead ID changes significantly (from a valid previous ID)
            if current_lead_id != self._lead_id_prev and self._lead_id_prev != -1:
                self._log(f"Lead ID changed: {self._lead_id_prev} -> {current_lead_id}.")
                self._reset_lead_emas(d_lead_raw, v_lead_raw, a_lead_raw)
            # Reset EMA if acquiring lead from no-lead state
            elif self._lead_id_prev == -1 and current_lead_id != -1:
                 self._log(f"Lead acquired: ID {current_lead_id}.")
                 self._reset_lead_emas(d_lead_raw, v_lead_raw, a_lead_raw)

            # Update filters with current raw data
            self._lead_drel_ema.update(d_lead_raw)
            self._lead_v_ema.update(v_lead_raw)
            self._lead_alead_ema.update(a_lead_raw)
            self._lead_absence_frames = 0
        else: # No lead currently
            self._lead_absence_frames += 1
            # If lead was just lost, reset EMAs to default values
            if self._lead_id_prev != -1:
                 self._log(f"Lead lost (Prev ID: {self._lead_id_prev}). Resetting lead EMAs to defaults.")
                 # Reset using the helper function ensures consistency
                 self._reset_lead_emas(AEM.LEAD_DIST_DEFAULT_NO_LEAD, self._v_ego_ema.x, 0.0)
            else: # Still no lead, continue updating towards defaults
                 self._lead_drel_ema.update(AEM.LEAD_DIST_DEFAULT_NO_LEAD)
                 self._lead_v_ema.update(self._v_ego_ema.x) # Update towards filtered ego speed
                 self._lead_alead_ema.update(0.0) # Update towards zero accel

        # Update previous lead ID for next cycle comparison *after* using it
        self._lead_id_prev = current_lead_id

        # 3. Get Filtered Values for decision making
        v_ego = self._v_ego_ema.x
        d_lead = self._lead_drel_ema.x
        v_lead = self._lead_v_ema.x # Filtered absolute lead speed
        a_lead = self._lead_alead_ema.x
        steering_angle_abs = self._steering_angle_abs_ema.x
        # v_model_error = self._v_model_error_ema.x

        ttc = self._calculate_ttc(d_lead, v_ego, v_lead)
        is_lead_one_vision_only = lead_status and current_lead_id == -1

        # 4. Infer Current Model Intentions from raw plan
        raw_model_stop_intention_current_cycle = False
        if model_path_plan_raw and 'v' in model_path_plan_raw:
            model_v_traj = model_path_plan_raw['v']
            if len(model_v_traj) >= 5:
                avg_final_model_v = np.mean(model_v_traj[-5:])
                final_model_v = model_v_traj[-1]
                # Consider stop if avg end speed is near creep and final point is very low
                raw_model_stop_intention_current_cycle = avg_final_model_v < AEM.SPEED_THRESHOLD_CREEP and \
                                                         final_model_v < AEM.SPEED_THRESHOLD_CREEP * 0.7
            elif len(model_v_traj) > 0: # Fallback for shorter trajectories
                raw_model_stop_intention_current_cycle = np.isclose(model_v_traj[-1], 0.0, atol=0.3)


        # --- 5. Mode Selection Logic ---
        min_prob_for_action = AEM.MIN_VISION_LEAD_PROB_ACTION if is_lead_one_vision_only else 0.0

        # A. Evaluate conditions to switch TO BLENDED (from ACC)
        if self._current_mode == 'acc':
            needs_blended_assist = False
            reason = "None" # Default reason

            # Scenario 1: Emergency/Dangerous Lead Situation
            if lead_status and ttc < AEM.LEAD_TTC_CRITICAL and v_ego > AEM.SPEED_THRESHOLD_LOW and model_prob_lead >= min_prob_for_action:
                needs_blended_assist, reason = True, f"Critical TTC ({ttc:.2f}s)"
            elif lead_status and a_lead < AEM.LEAD_ACCEL_HARD_BRAKE and d_lead < (v_ego * 2.5) and model_prob_lead >= min_prob_for_action:
                needs_blended_assist, reason = True, f"Hard lead brake ({a_lead:.2f}), d={d_lead:.1f}"
            elif fcw_active_prev:
                needs_blended_assist, reason = True, "FCW previously active"

            # Scenario 2: Sudden Lead Cut-in/Appearance
            # Check only if not already triggered by emergency
            if not needs_blended_assist and lead_status and current_lead_id != self._lead_id_prev and self._lead_id_prev != -1 and \
               ttc < AEM.LEAD_TTC_CAUTION and d_lead < (AEM.LEAD_DIST_VERY_CLOSE * 2.5) and model_prob_lead >= min_prob_for_action:
                needs_blended_assist, reason = True, f"Sudden cut-in (TTC={ttc:.2f}, d={d_lead:.1f})"

            # Scenario 3: Low-Speed/Urban/Congestion (Lead)
            if not needs_blended_assist and lead_status and v_ego < AEM.SPEED_THRESHOLD_LOW and \
               d_lead < (AEM.LEAD_DIST_VERY_CLOSE * 1.8) and model_prob_lead >= min_prob_for_action:
                needs_blended_assist, reason = True, f"Low speed ({v_ego:.1f}) close lead ({d_lead:.1f})"

            # Scenario 4: Model Predicts Stop
            if not needs_blended_assist and \
               (raw_model_stop_intention_current_cycle or model_predicts_stop_prev) and \
               v_ego > AEM.SPEED_THRESHOLD_CREEP:
                needs_blended_assist, reason = True, f"Model predicts stop (curr={raw_model_stop_intention_current_cycle}, prev={model_predicts_stop_prev})"

            # Scenario 5: High Curvature/Urban Turns (Curvature input removed)
            if not needs_blended_assist and \
               steering_angle_abs > AEM.STEERING_ANGLE_ABS_HIGH_CURVATURE and \
               v_ego < AEM.SPEED_THRESHOLD_CITY:
                needs_blended_assist, reason = True, f"High steering angle ({steering_angle_abs:.1f})"

            # Scenario 6: Planner Already Braking (Prev Cycle)
            if not needs_blended_assist and \
               a_target_from_prev_cycle < AEM.LEAD_ACCEL_MILD_BRAKE and \
               not standstill_raw and v_ego > AEM.SPEED_THRESHOLD_CREEP:
                needs_blended_assist, reason = True, f"Planner prev brake ({a_target_from_prev_cycle:.2f})"

            # Scenario 7: MPC Favored E2E (Prev Cycle) & still complex
            # if not needs_blended_assist and mpc_source_prev == 'e2e':
            #     is_complex = (v_ego < AEM.SPEED_THRESHOLD_CITY or \
            #                  (lead_status and ttc < AEM.LEAD_TTC_CAUTION and model_prob_lead >= min_prob_for_action) or \
            #                  (steering_angle_abs > AEM.STEERING_ANGLE_ABS_HIGH_CURVATURE * 0.6)) # Removed curvature check
            #     if is_complex:
            #         needs_blended_assist, reason = True, "Prev E2E source & ongoing complexity"

            # Scenario 8: High Gas Disengage Prob. (Model)
            if not needs_blended_assist and not allow_throttle_planner and \
               (not lead_status or (lead_status and v_ego < (v_lead + AEM.REL_SPEED_SIGNIFICANT_DIFFERENCE * 0.5))):
                 needs_blended_assist, reason = True, "Model advises against throttle"

            if needs_blended_assist:
                suggested_mode = 'blended'
                self._log(f"ACC->BLENDED Trigger: {reason}")


        # B. Evaluate conditions to switch TO ACC (from Blended)
        elif self._current_mode == 'blended':
            # Assume staying in blended unless conditions strongly favor returning to ACC
            suggested_mode = 'blended'

            # Check if any critical Blended-triggering condition is still fundamentally active
            blended_condition_still_active = False
            active_blended_reason = "None"
            if lead_status and ttc < AEM.LEAD_TTC_CRITICAL and v_ego > AEM.SPEED_THRESHOLD_LOW and model_prob_lead >= min_prob_for_action: blended_condition_still_active, active_blended_reason = True, "Critical TTC"
            elif lead_status and a_lead < AEM.LEAD_ACCEL_HARD_BRAKE and model_prob_lead >= min_prob_for_action: blended_condition_still_active, active_blended_reason = True, "Hard Lead Brake"
            elif fcw_active_prev: blended_condition_still_active, active_blended_reason = True, "Prev FCW"
            elif (raw_model_stop_intention_current_cycle or model_predicts_stop_prev) and v_ego > AEM.SPEED_THRESHOLD_CREEP: blended_condition_still_active, active_blended_reason = True, "Model Predicts Stop"
            elif steering_angle_abs > AEM.STEERING_ANGLE_ABS_HIGH_CURVATURE and v_ego < AEM.SPEED_THRESHOLD_CITY: blended_condition_still_active, active_blended_reason = True, "High Steering Angle"
            elif a_target_from_prev_cycle < AEM.LEAD_ACCEL_MILD_BRAKE and not standstill_raw and v_ego > AEM.SPEED_THRESHOLD_CREEP: blended_condition_still_active, active_blended_reason = True, "Prev Planner Brake"
            elif not allow_throttle_planner and (not lead_status or (lead_status and v_ego < (v_lead + AEM.REL_SPEED_SIGNIFICANT_DIFFERENCE * 0.5))): blended_condition_still_active, active_blended_reason = True, "Gas Disengage Prob"
            elif lead_status and v_ego < AEM.SPEED_THRESHOLD_LOW and \
                 d_lead < (AEM.LEAD_DIST_VERY_CLOSE * 1.8) and \
                 model_prob_lead >= min_prob_for_action: # Using filtered values (v_ego, d_lead) as in other checks
                blended_condition_still_active, active_blended_reason = True, f"Persisting Low speed ({v_ego:.1f}) close lead ({d_lead:.1f})"
            # Low speed congestion is implicitly handled by other checks usually involving TTC or stops

            if not blended_condition_still_active:
                safe_to_return_to_acc = False
                reason = ""
                # Scenario 10: Highway Cruising - Excellent Conditions
                if v_ego > AEM.SPEED_THRESHOLD_HIGHWAY and \
                   steering_angle_abs < (AEM.STEERING_ANGLE_ABS_HIGH_CURVATURE * 0.3) and \
                   (not lead_status or d_lead > (AEM.LEAD_DIST_FAR_HIGHWAY * 0.8) or ttc > (AEM.LEAD_TTC_CAUTION * 1.5)):
                    safe_to_return_to_acc, reason = True, "Highway cruise, clear path"
                # Scenario 11: Stable Following - Safe Distance Achieved
                elif lead_status and v_ego > AEM.SPEED_THRESHOLD_LOW and \
                     ttc > AEM.LEAD_TTC_CAUTION and \
                     d_lead > (AEM.LEAD_DIST_VERY_CLOSE * 2.0) and \
                     abs(a_lead) < (AEM.LEAD_ACCEL_PULLING_AWAY * 0.8) and \
                     abs(v_ego - v_lead) < (AEM.REL_SPEED_SIGNIFICANT_DIFFERENCE * 0.75) and \
                     steering_angle_abs < (AEM.STEERING_ANGLE_ABS_HIGH_CURVATURE * 0.5):
                    safe_to_return_to_acc, reason = True, f"Stable following (TTC={ttc:.2f}, d={d_lead:.1f})"
                # Scenario 12: Prolonged Lead Absence
                elif not lead_status:
                    personality_factor = 1.3 if long_personality == 0 else (0.7 if long_personality == 2 else 1.0)
                    fallback_frames = AEM.LEAD_LOST_FRAMES_TO_FALLBACK_BASE * personality_factor
                    if self._lead_absence_frames > fallback_frames and v_ego > AEM.SPEED_THRESHOLD_LOW:
                       safe_to_return_to_acc, reason = True, f"Prolonged lead absence ({self._lead_absence_frames} frames)"
                # Scenario 13: Persistent High Model Velocity Error
                # elif v_model_error > AEM.MODEL_VEL_ERROR_THRESHOLD:
                #     safe_to_return_to_acc, reason = True, f"High model vel error ({v_model_error:.2f})"
                # Default fallback if no active blended condition and no specific ACC condition met
                elif not safe_to_return_to_acc:
                    safe_to_return_to_acc, reason = True, "No active Blended condition found"

                if safe_to_return_to_acc:
                    suggested_mode = 'acc'
                    self._log(f"BLENDED->ACC Trigger: {reason}")
            else:
                 self._log(f"Staying BLENDED due to active condition: {active_blended_reason}")


        # --- 6. Apply Hysteresis ---
        if suggested_mode != self._current_mode:
            # If target mode changes, reset counter
            if self._target_mode_suggestion != suggested_mode:
                self._target_mode_suggestion = suggested_mode
                self._mode_switch_counter = 1
                self._log(f"Target mode suggestion: {self._target_mode_suggestion}. Counter: {self._mode_switch_counter}")
            # If target mode remains the same, increment counter
            else:
                self._mode_switch_counter += 1
                # Log counter increment for debugging flapping
                # self._log(f"Target mode {self._target_mode_suggestion} persists. Counter: {self._mode_switch_counter}")


            # Check if threshold is met to execute switch
            if self._mode_switch_counter >= AEM.HYSTERESIS_FRAMES_TO_SWITCH:
                self._log(f"\n-----------------------\nExecuting switch from {self._current_mode} to {self._target_mode_suggestion}\n-----------------------")
                self._current_mode = self._target_mode_suggestion
                # Reset hysteresis state after switch
                self._mode_switch_counter = 0
                self._target_mode_suggestion = None
            # else: remain in _current_mode until counter threshold met
        else: # Suggested mode is the same as current mode
            # If there was a pending switch suggestion, cancel it
            if self._target_mode_suggestion is not None:
                 self._log(f"Mode suggestion {suggested_mode} matches current mode {self._current_mode}, cancelling pending switch to {self._target_mode_suggestion}")
            # Reset hysteresis state
            self._target_mode_suggestion = None
            self._mode_switch_counter = 0

        # Return the potentially updated current mode
        return self._current_mode
