import { Navigate } from "react-router";

export default function NewVideoRedirect() { return <Navigate to="/video?new=1" replace />; }
