
import asyncio
import random

class MockState:
    def __init__(self):
        self.queue = []
        self.fetching_autoplay = False
        self.autoplay_dirty = False
        self.autoplay = True
        self.current_track = {'id': 'current'}
        self.history = []

state = MockState()

async def ensure_autoplay(force=False):
    global state
    if state.fetching_autoplay:
        if force: state.autoplay_dirty = True
        return
    
    # Check for suggestions
    suggestions = [t for t in state.queue if t.get('suggested')]
    if suggestions:
        if force or not state.queue[-1].get('suggested'):
            state.queue = [t for t in state.queue if not t.get('suggested')]
        else:
            return

    state.fetching_autoplay = True
    print(f"[Autoplay] Fetching... (force={force})")
    await asyncio.sleep(0.1) # Simulate network
    
    # Add suggestion
    state.queue = [t for t in state.queue if not t.get('suggested')]
    state.queue.append({'id': f'suggested_{random.randint(100,999)}', 'suggested': True})
    print(f"[Autoplay] Added suggestion. Queue: {[t['id'] for t in state.queue]}")
    
    state.fetching_autoplay = False
    if state.autoplay_dirty:
        state.autoplay_dirty = False
        asyncio.create_task(ensure_autoplay(force=True))

async def api_add(playlist_size):
    global state
    print(f"[API] Adding playlist of size {playlist_size}...")
    state.queue = [t for t in state.queue if not t.get('suggested')]
    
    await asyncio.sleep(0.2) # Simulate extract_info
    
    state.queue = [t for t in state.queue if not t.get('suggested')]
    
    new_tracks = [{'id': f'p_{i}'} for i in range(playlist_size)]
    state.queue.extend(new_tracks)
    print(f"[API] Added tracks. Queue: {[t['id'] for t in state.queue]}")
    
    asyncio.create_task(ensure_autoplay(force=True))

async def main():
    state.queue = [{'id': 'user_1'}]
    
    # Start an autoplay fetch
    t1 = asyncio.create_task(ensure_autoplay())
    await asyncio.sleep(0.05)
    
    # Start an API add that takes longer
    t2 = asyncio.create_task(api_add(3))
    
    await asyncio.gather(t1, t2)
    await asyncio.sleep(0.5) # Wait for all tasks
    print(f"Final Queue: {[t['id'] for t in state.queue]}")

if __name__ == "__main__":
    asyncio.run(main())
