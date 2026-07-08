import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createBrowserRouter,
  Navigate,
  RouterProvider,
} from "react-router-dom";

import "@fontsource-variable/fraunces/opsz.css";
import "@fontsource-variable/fraunces/opsz-italic.css";
import "@fontsource-variable/inter/index.css";
import "./styles/tokens.css";
import "./styles/base.css";
import "./components/ui.css";

import { Toaster } from "./components/Toaster";
import { AuthenticatedApp } from "./layouts/AuthenticatedApp";
import { CatalogPage } from "./pages/CatalogPage";
import { CookbookPage } from "./pages/CookbookPage";
import { InboxPage } from "./pages/InboxPage";
import { LoginPage } from "./pages/LoginPage";
import { RecipeDetailPage } from "./pages/RecipeDetailPage";
import { RecipeEditorPage } from "./pages/RecipeEditorPage";
import { RegisterPage } from "./pages/RegisterPage";
import { ReviewPage } from "./pages/ReviewPage";

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
});

const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  { path: "/register", element: <RegisterPage /> },
  {
    path: "/",
    element: <AuthenticatedApp />,
    children: [
      { index: true, element: <CookbookPage /> },
      { path: "inbox", element: <InboxPage /> },
      { path: "jobs/:id/review", element: <ReviewPage /> },
      { path: "recipes/new", element: <RecipeEditorPage /> },
      { path: "recipes/:id", element: <RecipeDetailPage /> },
      { path: "catalog", element: <CatalogPage /> },
    ],
  },
  { path: "*", element: <Navigate to="/" replace /> },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      <Toaster />
    </QueryClientProvider>
  </StrictMode>,
);
