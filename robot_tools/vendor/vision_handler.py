"""
Vision handling module
Captures images from Misty's camera and manages image processing
"""
import requests
import json
import os
import base64
import time


class VisionHandler:
    def __init__(self, robot, robot_ip):
        self.robot = robot
        self.robot_ip = robot_ip
        self.temp_image_dir = 'temp_images'
        self.temp_image_path = os.path.join(self.temp_image_dir, 'current_view.jpg')
        
        # Create temp directory if it doesn't exist
        os.makedirs(self.temp_image_dir, exist_ok=True)
    
    def capture_image(self):
        """Capture an image from Misty's camera"""
        print("Capturing image from Misty's camera...")
        
        try:
            time.sleep(2)
            
            # Camera aspect (verified on this unit): Misty returns the *transpose* of the
            # requested size (1600x1200 -> a 1200x1600 frame) with content UPRIGHT (no EXIF, so
            # do NOT post-rotate — rotating makes it sideways). Only the exact resolutions in
            # Misty's list are valid; a non-listed size like 1080x1920 makes take_picture return
            # nothing (empty frame). The camera is effectively portrait-native, so resolution
            # can't produce a landscape frame — a 16:9 request just comes back narrower. We use
            # 1600x1200 (valid; widest resulting portrait). To get a landscape-shaped image you'd
            # crop, not rescale. Valid sizes: docs.mistyrobotics.com/.../#take_picture
            # Confirmed on this unit: the camera is portrait-native. Any request comes back as a
            # transposed PORTRAIT frame (1600x1200 -> 1200x1600; 1920x1080 -> 1080x1920), content
            # upright (no EXIF; do NOT rotate). 16:9 only makes it narrower (1080px vs 1200px
            # wide), so 4:3 (1600x1200) gives the widest usable frame. Resolution can't produce a
            # landscape image — a center-crop would, at the cost of vertical FOV.
            result = self.robot.take_picture(
                base64=True,
                fileName='vision_temp',
                width=1600,
                height=1200,
                displayOnScreen=False,
                overwriteExisting=True
            )
            
            # Extract filename from response
            if hasattr(result, 'json'):
                response_data = result.json()
            elif hasattr(result, 'text'):
                response_data = json.loads(result.text)
            else:
                response_data = result
            
            if 'result' in response_data:
                filename = response_data['result']['name']
            else:
                filename = response_data.get('name')
            
            print(f"Image captured: {filename}")
            return filename
            
        except Exception as e:
            print(f"Error capturing image: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def download_image(self, filename):
        """Download image from Misty and return as base64"""
        try:
            # Download the image
            image_url = f"http://{self.robot_ip}/api/images?FileName={filename}"
            response = requests.get(image_url)
            
            if response.status_code == 200:
                # Save temporarily
                with open(self.temp_image_path, 'wb') as f:
                    f.write(response.content)
                
                # Read and encode to base64
                with open(self.temp_image_path, 'rb') as f:
                    image_data = base64.b64encode(f.read()).decode('utf-8')
                
                return image_data
            else:
                print(f"Failed to download image: {response.status_code}")
                return None
                
        except Exception as e:
            print(f"Error downloading image: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def capture_and_encode(self):
        """Capture image and return as base64 string"""
        filename = self.capture_image()
        if filename:
            return self.download_image(filename)
        return None
    
    def cleanup(self):
        """Clean up temporary image files"""
        if os.path.exists(self.temp_image_path):
            os.remove(self.temp_image_path)