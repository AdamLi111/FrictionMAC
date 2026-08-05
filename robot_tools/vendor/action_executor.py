"""
VENDORED from EmbodiedPF/model/action_executor.py, narration STRIPPED.
Source of truth for the calibration math (_calculate_drive_time /
_calculate_turn_time). Tool wrappers reuse those helpers and issue
robot.drive_time(...) directly; nothing here narrates.

Action executor module
Defines and executes all robot actions with positive friction support and spatial navigation
Now includes 360-degree object finding capability
"""
import time
import math


class ActionExecutor:
    def __init__(self, robot, vision_handler=None, llm_layer=None, logger=None):
        self.robot = robot
        self.vision_handler = vision_handler
        self.llm_layer = llm_layer
        self.logger = logger
        
        # Action mapping
        self.actions = {
            # Regular movements
            'forward': self.move_forward,
            'backward': self.move_backward,
            'left': self.move_left,
            'right': self.move_right,
            'turn_left': self.turn_left,
            'turn_right': self.turn_right,
            'stop': self.stop,

            # Spoken actions
            'speak': self.speak,
            'clarify': self.clarify,

            # Visual parsing
            'describe_vision': self.describe_vision,
            
            # Object finding
            'find_object': self.find_object,

            # Spatial navigation
            'spatial_navigate': self.spatial_navigate,
            'unknown': self.unknown_action
        }

    def _speak(self, text):
        # Narration stripped in the tool layer -- tools never talk.
        # speak()/ask_clarification() are the only tools that talk.
        return None
    
    def execute(self, intent):
        """Execute an action based on parsed intent with friction handling"""
        # Handle multi-action sequences (only if array is non-empty)
        if 'actions' in intent and len(intent.get('actions', [])) > 0:
            friction_type = intent.get('friction_type', 'none')
            text = intent.get('text', '')
            
            # Announce the plan if there's text
            if text:
                print(f"Announcing plan: {text}")
                pass  # narration stripped in tool layer
                time.sleep(0.5)
            
            print(f"Executing multi-action sequence")
            for action_item in intent['actions']:
                action = action_item.get('action', 'unknown')
                distance = action_item.get('distance', 1.0)
                turn_degrees = action_item.get('turn_degrees', 0)
                target_object = action_item.get('target_object', None)
                
                action_func = self.actions.get(action, self.unknown_action)
                
                # Call the appropriate function based on action type
                if action == 'turn_left' or action == 'turn_right':
                    execution_time = action_func(degrees=turn_degrees, speak=True)
                elif action == 'spatial_navigate':
                    execution_time = action_func(target_object=target_object, distance=distance, turn_degrees=turn_degrees)
                elif action == 'find_object':
                    execution_time = action_func(target_object=target_object)
                else:
                    execution_time = action_func(distance=distance, turn_degrees=turn_degrees)
                
                # Small delay between actions
                time.sleep(execution_time / 1000 + 0.5)
            return
        
        # Single action handling
        friction_type = intent.get('friction_type', 'none')
        action = intent.get('action', 'unknown')
        distance = intent.get('distance', 1.0)
        text = intent.get('text', '')
        target_object = intent.get('target_object', None)
        turn_degrees = intent.get('turn_degrees', 0)
        
        print(f"Executing - Friction: {friction_type}, Action: {action}")
        
        # Handle friction types
        if friction_type != 'none' and text:
            print(f"Applying {friction_type} friction: {text}")

            if action != 'speak':
                pass  # narration stripped in tool layer
            # Probing stops execution to wait for user response
            if friction_type == 'probing' or action == 'clarify':
                return
            
            # Reflective pause with delay before continuing
            if friction_type == 'reflective_pause':
                time.sleep(1.5)
            
            # For other friction types (assumption_reveal, overspecification, reinforcement),
            # we announce and then continue with the action
            time.sleep(0.5)
        
        # Get the action function and execute it
        action_func = self.actions.get(action, self.unknown_action)
        
        if action == 'spatial_navigate':
            # Pass all spatial navigation parameters
            action_func(
                target_object=target_object,
                distance=distance,
                turn_degrees=turn_degrees
            )
        elif action == 'find_object':
            action_func(target_object=target_object)
        elif action == 'turn_left' or action == 'turn_right':
            action_func(degrees=turn_degrees, speak=True)
        else:
            action_func(distance=distance, text=text)
    
    def find_object(self, target_object=None, **kwargs):
        """
        Find an object by capturing 4 images in a 360-degree scan
        Sends all images to VLM for analysis and location description
        """
        if not target_object:
            print("No target object specified for find action")
            pass  # narration stripped in tool layer
            return
        
        if not self.vision_handler or not self.llm_layer:
            print("Vision or LLM handler not available")
            pass  # narration stripped in tool layer
            return
        
        print(f"Starting 360-degree scan to find: {target_object}")
        pass  # narration stripped in tool layer
        
        # Capture 4 images at 90-degree intervals
        images = []
        directions = ["front", "left", "back", "right"]
        
        for i, direction in enumerate(directions):
            print(f"Capturing image {i+1}/4 - {direction} view")
            
            # Capture and encode image
            image_data = self.vision_handler.capture_and_encode()
            
            if image_data:
                images.append({
                    'direction': direction,
                    'data': image_data
                })
                print(f"✓ Captured {direction} view")
            else:
                print(f"✗ Failed to capture {direction} view")
            
            # Turn 90 degrees left for next view (except on last iteration)
            if i < 3:
                print(f"Turning left 90 degrees for next view...")
                self.turn_left(degrees=90, speak=False)
                time.sleep(2)  # Wait for turn to complete and camera to stabilize
        
        # Turn back to face forward (270 degrees left = 90 degrees right)
        print("Returning to original position...")
        self.turn_left(degrees=90, speak=False)
        time.sleep(2)

        total_turn_time = int(self._calculate_turn_time(90) * 3 + 6)
        
        # Check if we got at least one image
        if not images:
            print("Failed to capture any images")
            pass  # narration stripped in tool layer
            return 
        
        print(f"Captured {len(images)} images, analyzing with VLM...")
        pass  # narration stripped in tool layer
        
        # Call VLM to analyze all images
        result = self.llm_layer.find_object_in_images(target_object, images)
        
        if result:
            response_text = result.get('response', '')
            found = result.get('found', False)
            
            if found:
                print(f"Object found! Response: {response_text}")
            else:
                print(f"Object not found. Response: {response_text}")
            
            # Speak the result
            pass  # narration stripped in tool layer
        else:
            print("VLM analysis failed")
            pass  # narration stripped in tool layer

        return total_turn_time
    
    def spatial_navigate(self, target_object=None, distance=0, turn_degrees=0, **kwargs):
        """
        Navigate to a target object using pre-analyzed vision data
        Vision analysis has already been done by the LLM layer, so we just execute the plan
        """
        if not target_object:
            print("No target object specified")
            pass  # narration stripped in tool layer
            return
        
        print(f"Executing spatial navigation towards: {target_object}")
        print(f"Navigation plan - Turn: {turn_degrees}°, Distance: {distance}m")
        turn_time = self._calculate_turn_time(turn_degrees)
        
        # Announce the plan
        if turn_degrees == 0:
            pass  # narration stripped in tool layer
        elif turn_degrees < 0:
            pass  # narration stripped in tool layer
        else:
            pass  # narration stripped in tool layer
        
        time.sleep(1)
        
        # Execute the turn if needed using refactored turn methods
        if turn_degrees != 0:
            if turn_degrees < 0:  # Left turn
                self.turn_left(degrees=abs(turn_degrees), speak=False)
            else:  # Right turn
                self.turn_right(degrees=turn_degrees, speak=False)
        
        # Move forward if distance > 0
        if distance > 0:
            # Reduce distance slightly to avoid collision
            safe_distance = max(0.3, distance - 0.5)
            drive_time = self._calculate_drive_time(safe_distance)
            print(f"Moving forward {safe_distance:.1f} meters")
            self.robot.drive_time(50, 0, drive_time)
            
            time.sleep(drive_time / 1000 + 0.5)
            
            pass  # narration stripped in tool layer

        return int(turn_degrees + turn_time + 1)
    
    def move_forward(self, distance=1.0, **kwargs):
        """Move forward by specified distance"""
        print(f"Moving forward {distance} meter(s)")
        pass  # narration stripped in tool layer
        drive_time = self._calculate_drive_time(distance)
        self.robot.drive_time(50, 0, drive_time)
        return drive_time
    
    def move_backward(self, distance=1.0, **kwargs):
        """Move backward by specified distance"""
        print(f"Moving backward {distance} meter(s)")
        pass  # narration stripped in tool layer
        drive_time = self._calculate_drive_time(distance)
        self.robot.drive_time(-50, 0, drive_time)
        return drive_time
    
    def move_left(self, distance=1.0, **kwargs):
        """Strafe left by specified distance"""
        print(f"Going left {distance} meter(s)")
        pass  # narration stripped in tool layer
        # Turn left, move forward, turn back right
        self.robot.drive_time(0, -100, 2150)  # ~45 degrees
        time.sleep(2.5)
        drive_time = self._calculate_drive_time(distance)
        self.robot.drive_time(50, 0, drive_time)
        time.sleep(drive_time / 1000 + 0.5)
        self.robot.drive_time(0, 100, 2150)  # Turn back
        return drive_time
    
    def move_right(self, distance=1.0, **kwargs):
        """Strafe right by specified distance"""
        print(f"Going right {distance} meter(s)")
        pass  # narration stripped in tool layer
        # Turn right, move forward, turn back left
        self.robot.drive_time(0, 100, 2150)  # ~45 degrees
        time.sleep(2.5)
        drive_time = self._calculate_drive_time(distance)
        self.robot.drive_time(50, 0, drive_time)
        time.sleep(drive_time / 1000 + 0.5)
        self.robot.drive_time(0, -100, 2150)  # Turn back
        return drive_time
    
    def turn_left(self, degrees, speak=True, **kwargs):
        if not degrees:
            print("no turn degree specified")
        """Turn left by specified degrees (default 90)"""
        print(f"Turning left {degrees} degrees")
        if speak:
            pass  # narration stripped in tool layer
        
        turn_time = self._calculate_turn_time(degrees) - 100 if degrees >= 90 else self._calculate_turn_time
        self.robot.drive_time(0, 100, turn_time)
        return turn_time
    
    def turn_right(self, degrees, speak=True, **kwargs):
        if not degrees:
            print("no turn degree specified")
        """Turn right by specified degrees (default 90)"""
        print(f"Turning right {degrees} degrees")
        if speak:
            pass  # narration stripped in tool layer
        
        turn_time = self._calculate_turn_time(degrees) - 60 if degrees >= 90 else self._calculate_turn_time
        self.robot.drive_time(0, -100, turn_time)
        return turn_time
    
    def stop(self, **kwargs):
        """Stop all movement"""
        print("Stopping")
        pass  # narration stripped in tool layer
        self.robot.stop()
    
    def speak(self, text='', **kwargs):
        """Speak the provided text"""
        if text:
            print(f"Speaking: {text}")
            pass  # narration stripped in tool layer
    
    def clarify(self, text='', **kwargs):
        """Request clarification from user"""
        print(f"Requesting clarification: {text}")
        pass  # narration stripped in tool layer
    
    def describe_vision(self, **kwargs):
        """Capture image and describe what robot sees"""
        if not self.vision_handler or not self.llm_layer:
            print("Vision or LLM handler not available")
            pass  # narration stripped in tool layer
            return
        
        print("Processing vision request...")
        pass  # narration stripped in tool layer
        
        # Capture and encode image
        image_data = self.vision_handler.capture_and_encode()
        
        if image_data:
            # Get description from LLM
            description = self.llm_layer.describe_image(image_data)
            
            if description:
                print(f"Vision description: {description}")
                pass  # narration stripped in tool layer
            else:
                pass  # narration stripped in tool layer
        else:
            pass  # narration stripped in tool layer
    
    def unknown_action(self, **kwargs):
        """Handle unknown or unrecognized actions"""
        print("Command not recognized")
        pass  # narration stripped in tool layer
    
    def _calculate_drive_time(self, distance):
        """Calculate drive time in milliseconds based on distance
        
        The robot's driving acceleration is non-linear - it starts slower and 
        speeds up over time. Need to balance giving more time for both short 
        distances (due to acceleration) and long distances.
        
        Calibration: Needs testing, but using base of 2500ms per meter
        Using power relationship: time = k * distance^exponent
        Exponent = 0.7 balances short/long distance needs
        """
        exponent = 0.5  # Adjust between 0.5-1.0 based on testing
        k = 2500 / math.pow(1, exponent)  # Base calibration constant
        return int(k * math.pow(distance, exponent))
    
    def _calculate_turn_time(self, degrees):
        if degrees < 30:
            # Linear relationship for small turns
            # Calibrate: measure actual times for 5°, 10°, 15°, 20°, 25°
            return int(degrees * 100)  # adjust multiplier from testing
        else:
            # Square root for larger turns
            k = 4500 / math.sqrt(90)
            return int(k * math.sqrt(degrees))