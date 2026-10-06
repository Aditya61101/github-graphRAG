import { RouterProvider } from "react-router";

import { appRouter } from "@/routers/app-router";

export default function App() {
  return <RouterProvider router={appRouter} />;
}
