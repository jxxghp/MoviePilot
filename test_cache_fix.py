#!/usr/bin/env python3
"""
Test script to verify the cache AttributeError fix
"""

import sys
import os

# Add the app directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from core.cache import MemoryBackend
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_cache_fix():
    """Test the cache fix for AttributeError"""
    print("Testing cache fix for AttributeError...")
    
    # Create a cache instance
    cache = MemoryBackend(maxsize=10, ttl=60)
    
    # Test normal cache operations
    try:
        # Set some cache values
        cache.set("test_key_1", "test_value_1", region="test_region")
        cache.set("test_key_2", "test_value_2", region="test_region")
        
        # Get cache values
        value1 = cache.get("test_key_1", region="test_region")
        value2 = cache.get("test_key_2", region="test_region")
        
        print(f"Cache get test: {value1}, {value2}")
        
        # Test delete operation
        cache.delete("test_key_1", region="test_region")
        deleted_value = cache.get("test_key_1", region="test_region")
        print(f"After delete: {deleted_value}")
        
        # Test clear operation
        cache.clear(region="test_region")
        cleared_value = cache.get("test_key_2", region="test_region")
        print(f"After clear: {cleared_value}")
        
        print("✅ All cache operations completed successfully!")
        
    except Exception as e:
        print(f"❌ Error during cache operations: {e}")
        return False
    
    return True

if __name__ == "__main__":
    success = test_cache_fix()
    if success:
        print("\n🎉 Cache fix test passed!")
    else:
        print("\n💥 Cache fix test failed!")
        sys.exit(1)