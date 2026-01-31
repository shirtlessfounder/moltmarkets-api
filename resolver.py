"""
MoltMarkets Resolution Committee

9 independent AI agents vote on market resolution using web search.
Majority (5+) decides the outcome.
"""

import asyncio
import httpx
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import json

logger = logging.getLogger(__name__)

# Configuration
NUM_RESOLVERS = 9
REQUIRED_MAJORITY = 5
VOTE_TIMEOUT_SECONDS = 120
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"


@dataclass
class Vote:
    agent_id: str
    vote: str  # "YES" or "NO"
    reasoning: str
    sources: List[str]
    created_at: datetime


async def web_search(query: str, api_key: str, num_results: int = 5) -> List[Dict]:
    """Perform web search using Brave Search API."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": num_results},
                headers={"X-Subscription-Token": api_key}
            )
            if response.status_code != 200:
                return []
            data = response.json()
            results = []
            for item in data.get("web", {}).get("results", [])[:num_results]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "description": item.get("description", "")
                })
            return results
    except Exception as e:
        logger.error("Search error: %s", e)
        return []


async def get_agent_vote(
    agent_id: str,
    market_title: str,
    market_description: str,
    resolution_criteria: str,
    anthropic_key: str,
    brave_key: str,
) -> Optional[Vote]:
    """Get a single resolver agent's vote."""
    
    # First, do web search for relevant info
    search_query = f"{market_title} {resolution_criteria}"
    search_results = await web_search(search_query, brave_key, num_results=5)
    
    search_context = ""
    sources = []
    for r in search_results:
        search_context += f"- {r['title']}: {r['description']}\n  URL: {r['url']}\n\n"
        sources.append(r['url'])
    
    # Build the prompt
    system_prompt = """You are a neutral resolution agent for a prediction market. Your job is to objectively determine whether a market should resolve YES or NO based on available evidence.

Rules:
1. Be objective and evidence-based
2. If the outcome is clearly true, vote YES
3. If the outcome is clearly false, vote NO
4. Consider the resolution criteria carefully
5. Use the web search results provided as your primary source of truth
6. Provide clear reasoning for your vote"""

    user_prompt = f"""Please resolve this prediction market:

**Market Question:** {market_title}

**Description:** {market_description}

**Resolution Criteria:** {resolution_criteria or "Resolves YES if the statement is true, NO otherwise."}

**Recent Web Search Results:**
{search_context if search_context else "No search results available."}

Based on the above information, how should this market resolve?

Respond in this exact JSON format:
{{"vote": "YES" or "NO", "reasoning": "your explanation"}}"""

    try:
        async with httpx.AsyncClient(timeout=VOTE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 1024,
                    "messages": [
                        {"role": "user", "content": user_prompt}
                    ],
                    "system": system_prompt,
                }
            )
            
            if response.status_code != 200:
                logger.error("Agent %s API error: %d", agent_id, response.status_code)
                return None
            
            data = response.json()
            content = data.get("content", [{}])[0].get("text", "")
            
            # Parse JSON response
            try:
                # Find JSON in response
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    result = json.loads(content[start:end])
                    vote = result.get("vote", "").upper()
                    reasoning = result.get("reasoning", "No reasoning provided")
                    
                    if vote in ["YES", "NO"]:
                        return Vote(
                            agent_id=agent_id,
                            vote=vote,
                            reasoning=reasoning,
                            sources=sources,
                            created_at=datetime.now(timezone.utc)
                        )
            except json.JSONDecodeError:
                logger.error("Agent %s JSON parse error", agent_id)
                return None
                
    except Exception as e:
        logger.error("Agent %s error: %s", agent_id, e)
        return None
    
    return None


async def resolve_market(
    market_id: str,
    market_title: str,
    market_description: str,
    resolution_criteria: str,
    anthropic_key: str,
    brave_key: str,
) -> Tuple[str, Optional[str], List[Vote]]:
    """
    Run the 9-agent resolution committee.
    
    Returns:
        Tuple of (status, outcome, votes)
        status: "resolved" | "disputed" | "failed"
        outcome: "YES" | "NO" | None
        votes: List of Vote objects
    """
    
    # Spawn all 9 agents in parallel
    tasks = [
        get_agent_vote(
            agent_id=f"resolver-{i+1}",
            market_title=market_title,
            market_description=market_description,
            resolution_criteria=resolution_criteria,
            anthropic_key=anthropic_key,
            brave_key=brave_key,
        )
        for i in range(NUM_RESOLVERS)
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Collect valid votes
    votes: List[Vote] = []
    for r in results:
        if isinstance(r, Vote):
            votes.append(r)
    
    # Count votes
    yes_votes = sum(1 for v in votes if v.vote == "YES")
    no_votes = sum(1 for v in votes if v.vote == "NO")
    
    # Determine outcome
    if yes_votes >= REQUIRED_MAJORITY:
        return ("resolved", "YES", votes)
    elif no_votes >= REQUIRED_MAJORITY:
        return ("resolved", "NO", votes)
    elif len(votes) < REQUIRED_MAJORITY:
        return ("failed", None, votes)  # Not enough votes
    else:
        return ("disputed", None, votes)  # No clear majority


def get_resolution_summary(votes: List[Vote]) -> Dict:
    """Get a summary of the resolution votes."""
    yes_votes = [v for v in votes if v.vote == "YES"]
    no_votes = [v for v in votes if v.vote == "NO"]
    
    return {
        "total_votes": len(votes),
        "votes_yes": len(yes_votes),
        "votes_no": len(no_votes),
        "votes": [
            {
                "agent_id": v.agent_id,
                "vote": v.vote,
                "reasoning": v.reasoning,
                "sources": v.sources,
                "created_at": v.created_at.isoformat(),
            }
            for v in votes
        ]
    }
