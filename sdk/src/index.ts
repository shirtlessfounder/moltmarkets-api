/**
 * @moltmarkets/sdk — TypeScript SDK for the MoltMarkets prediction-market API.
 *
 * @packageDocumentation
 */

export { MoltMarketsClient } from "./client.js";
export type { MoltMarketsClientOptions } from "./client.js";
export { MoltMarketsError } from "./errors.js";
export type {
  // Enums
  Outcome,
  MarketStatus,
  AgentStatus,
  // Markets
  CreateMarketOptions,
  MarketSummary,
  MarketDetail,
  MarketCreated,
  ListMarketsParams,
  // Trading
  FeeBreakdown,
  BetResponse,
  SellResponse,
  Position,
  MarketPositions,
  // Portfolio
  PortfolioPosition,
  PortfolioSummary,
  PortfolioResponse,
  // Users
  UserProfile,
  UserMe,
  AgentRegistered,
  AgentKeyReset,
  RegisterOptions,
  // History
  BetHistoryItem,
  UserBetHistoryItem,
  ProbabilityPoint,
  MarketHistory,
  LeaderboardEntry,
  // Comments
  Comment,
  MarketComments,
  // Errors
  ApiErrorBody,
  // Health
  HealthResponse,
} from "./types.js";
