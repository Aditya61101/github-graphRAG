import type {
  ExploreChatMessage,
  SendExploreMessagePayload,
} from "@/types/explore";

export const exploreService = {
  sendMessage: async ({
    message,
  }: SendExploreMessagePayload): Promise<ExploreChatMessage> => {
    const response = await new Promise<ExploreChatMessage>((resolve) => {
      setTimeout(() => {
        resolve({
          id: Date.now(),
          role: "assistant",
          content: `Mock response for: ${message}. Backend GraphRAG integration can now be connected here.`,
        });
      }, 2000);
    });

    return response;
  },
};
