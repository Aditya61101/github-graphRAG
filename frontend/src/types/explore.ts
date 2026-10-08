export type ExploreChatMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
};

export type SendExploreMessagePayload = {
  message: string;
};
