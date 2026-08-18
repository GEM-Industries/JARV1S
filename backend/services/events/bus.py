import logging
from typing import Dict, List, Callable, Awaitable, Tuple
import asyncio
from collections import defaultdict

from .models import Event
from .types import EventType

logger = logging.getLogger(__name__)

EventHandler = Callable[[Event], Awaitable[None]]

class EventBus:
    """Event bus for handling system-wide events."""
    
    def __init__(self):
        self._subscribers: Dict[str, List[EventHandler]] = defaultdict(list)
        self._wildcard_subscribers: List[Tuple[str, EventHandler]] = []
        self._running = False
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
    
    async def start(self):
        """Start the event bus."""
        if self._running:
            return
        
        self._running = True
        logger.info("Event bus started")
        
        # Start processing events
        asyncio.create_task(self._process_events())
    
    async def stop(self):
        """Stop the event bus."""
        if not self._running:
            return
        
        self._running = False
        logger.info("Event bus stopped")
    
    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribers."""
        if not self._running:
            logger.warning("Attempted to publish event while event bus is not running")
            return
        
        await self._queue.put(event)
    
    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """
        Subscribe to events. Supports wildcards (e.g., 'voice.*').
        """
        if "*" in event_type:
            # Store prefix without the wildcard for faster matching
            # e.g., "voice.*" -> "voice."
            prefix = event_type.rstrip("*")
            self._wildcard_subscribers.append((prefix, handler))
            logger.debug(f"Wildcard handler subscribed to {event_type}")
        else:
            self._subscribers[event_type].append(handler)
            logger.debug(f"Handler subscribed to {event_type}")
    
    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Unsubscribe from events."""
        if "*" in event_type:
            prefix = event_type.rstrip("*")
            try:
                self._wildcard_subscribers.remove((prefix, handler))
                logger.debug(f"Wildcard handler unsubscribed from {event_type}")
            except ValueError:
                logger.warning(f"Wildcard handler not found for {event_type}")
        elif event_type in self._subscribers:
            try:
                self._subscribers[event_type].remove(handler)
                logger.debug(f"Handler unsubscribed from {event_type}")
            except ValueError:
                logger.warning(f"Handler not found for {event_type}")
    
    async def _process_events(self) -> None:
        """Process events from the queue."""
        while self._running:
            try:
                event = await self._queue.get()
                
                # 1. Get exact matches
                handlers = list(self._subscribers[event.type])
                
                # 2. Get wildcard matches
                # Match if event type starts with the prefix
                for prefix, handler in self._wildcard_subscribers:
                    if event.type.startswith(prefix):
                        handlers.append(handler)
                
                if not handlers:
                    continue
                
                # Process all handlers concurrently
                tasks = [
                    asyncio.create_task(self._execute_handler(handler, event))
                    for handler in handlers
                ]
                
                await asyncio.gather(*tasks, return_exceptions=True)
                self._queue.task_done()
                
            except Exception as e:
                logger.error(f"Error processing event: {e}")
    
    async def _execute_handler(self, handler: EventHandler, event: Event) -> None:
        """Execute a single event handler safely."""
        try:
            await handler(event)
        except Exception as e:
            logger.error(f"Error in event handler: {e}")

# Create global event bus instance
event_bus: EventBus = EventBus() 