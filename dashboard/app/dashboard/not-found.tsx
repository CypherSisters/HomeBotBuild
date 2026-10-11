import Link from "next/link";
import CardBox from "@/app/_components/CardBox";
import { mdiMapMarkerQuestion } from "@mdi/js";
import Icon from "@/app/_components/Icon";

export default function DashboardNotFound() {
  return (
    <div className="flex items-center justify-center min-h-[60vh] p-6">
      <CardBox className="max-w-md w-full text-center p-8">
        <Icon path={mdiMapMarkerQuestion} size={48} className="text-[#00B4D8] mx-auto mb-4" />
        <h2 className="text-xl font-bold mb-2">Page Not Found</h2>
        <p className="text-gray-600 dark:text-gray-400 mb-4">
          The page you&apos;re looking for doesn&apos;t exist or has been moved.
        </p>
        <Link
          href="/dashboard"
          className="inline-block px-6 py-2 bg-[#00B4D8] text-white rounded-lg hover:bg-[#0096C7] transition-colors"
        >
          Back to Dashboard
        </Link>
      </CardBox>
    </div>
  );
}
