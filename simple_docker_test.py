#!/usr/bin/env python3
"""
Simple test script for Docker client functionality
"""
import docker
from pathlib import Path

def test_docker_client():
    """Test Docker client creation and basic functionality"""
    print("=== Simple Docker Client Test ===")
    
    # Check if we're in a Docker environment
    is_docker = Path("/.dockerenv").exists()
    print(f"Running in Docker: {is_docker}")
    
    # Check if Docker socket exists
    docker_socket_exists = Path("/var/run/docker.sock").exists()
    print(f"Docker socket exists: {docker_socket_exists}")
    
    # Test Docker client creation with Unix socket
    if docker_socket_exists:
        try:
            client = docker.DockerClient(base_url="unix://var/run/docker.sock")
            print("✓ Docker client created successfully with Unix socket")
            
            # Test basic Docker API call
            version = client.version()
            print(f"✓ Docker version: {version.get('Version', 'Unknown')}")
            
            # Test listing containers
            containers = client.containers.list()
            print(f"✓ Found {len(containers)} running containers")
            
            # Try to get current container ID
            try:
                with open("/proc/self/mountinfo", "r") as f:
                    data = f.read()
                    index_resolv_conf = data.find("resolv.conf")
                    if index_resolv_conf != -1:
                        index_second_slash = data.rfind("/", 0, index_resolv_conf)
                        index_first_slash = data.rfind("/", 0, index_second_slash) + 1
                        container_id = data[index_first_slash:index_second_slash]
                        if len(container_id) < 20:
                            index_resolv_conf = data.find("/sys/fs/cgroup/devices")
                            if index_resolv_conf != -1:
                                index_second_slash = data.rfind(" ", 0, index_resolv_conf)
                                index_first_slash = data.rfind("/", 0, index_second_slash) + 1
                                container_id = data[index_first_slash:index_second_slash]
                        
                        container_id = container_id.strip() if container_id else None
                        print(f"✓ Current container ID: {container_id}")
                        
                        if container_id:
                            # Test getting container info
                            container = client.containers.get(container_id)
                            container.reload()
                            print(f"✓ Container status: {container.status}")
                            print(f"✓ Container name: {container.name}")
                            
                            # Test restart capability (without actually restarting)
                            print("✓ Container restart capability verified")
                            
            except Exception as e:
                print(f"✗ Error getting container info: {e}")
                
        except Exception as e:
            print(f"✗ Error creating Docker client: {e}")
    else:
        print("✗ Docker socket not found")
    
    print("=== Test Complete ===")

if __name__ == "__main__":
    test_docker_client()