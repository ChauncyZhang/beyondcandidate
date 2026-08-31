import React from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { App } from "./App.jsx";
import { AppRouteError } from "./AppRouteError.jsx";
import "./styles.css";
import "./product-theme.css";
import "./product-theme-interviews.css";

const router = createBrowserRouter([{ path: "*", element: <App />, errorElement: <AppRouteError /> }]);

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
