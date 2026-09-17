import { Navigate } from "react-router";

export default function NewDownloadRedirect() { return <Navigate to="/downloads?new=1" replace />; }
