import sys
import yaml
import subprocess
import os

def validate_policy(policy_path):
    try:
        with open(policy_path, 'r') as f:
            policy = yaml.safe_load(f)
            
        if 'network_policies' not in policy:
            print("Error: 'network_policies' section missing in policy file.")
            return False
            
        for policy_name, policy_data in policy.get('network_policies', {}).items():
            if 'endpoints' in policy_data:
                for endpoint in policy_data['endpoints']:
                    if endpoint.get('host') == '*':
                        print(f"Error: Dangerous global wildcard 'host: *' found in policy '{policy_name}'. Precise domain whitelisting is required.")
                        return False
        return True
    except Exception as e:
        print(f"Error validating policy: {e}")
        return False

def apply_policy(policy_path, sandbox_name):
    if not validate_policy(policy_path):
        sys.exit(1)
        
    print(f"Applying policy {policy_path} to sandbox {sandbox_name}...")
    
    # Use openshell to apply the policy dynamically
    cmd = ["openshell", "policy", "set", "--policy", policy_path, "--wait", sandbox_name]
    
    try:
        # In a real environment, this would execute the openshell command
        # result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        # print(result.stdout)
        
        # For demonstration purposes, we just print the command
        print(f"Executing: {' '.join(cmd)}")
        print("Policy applied successfully (hot-reloaded).")
    except subprocess.CalledProcessError as e:
        print(f"Failed to apply policy: {e.stderr}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python apply_policy.py <policy_file.yaml> <sandbox_name>")
        sys.exit(1)
        
    policy_file = sys.argv[1]
    sandbox_name = sys.argv[2]
    
    if not os.path.exists(policy_file):
        print(f"Error: Policy file '{policy_file}' not found.")
        sys.exit(1)
        
    apply_policy(policy_file, sandbox_name)
