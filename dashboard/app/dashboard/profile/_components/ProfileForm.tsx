"use client";

import { useState } from "react";
import {
  mdiAccount,
  mdiMail,
  mdiContentSave,
  mdiCheckCircle,
  mdiAlertCircle,
  mdiLoading,
} from "@mdi/js";
import { Formik, Form, Field } from "formik";
import Button from "../../../_components/Button";
import Buttons from "../../../_components/Buttons";
import CardBox from "../../../_components/CardBox";
import CardBoxComponentBody from "../../../_components/CardBox/Component/Body";
import CardBoxComponentFooter from "../../../_components/CardBox/Component/Footer";
import FormField from "../../../_components/FormField";
import Icon from "../../../_components/Icon";
import AvatarPicker from "../../_components/AvatarPicker";
import { useHousehold } from "../../../_lib/hooks/useHousehold";
import { avatarSrc } from "../../../_lib/services/householdService";
import { updateProfile } from "../../../_lib/services/profileService";
import type { UserProfile } from "../../../_interfaces/api";

type Props = {
  profile: UserProfile;
  onSaved: (updated: UserProfile) => void;
};

export default function ProfileForm({ profile, onSaved }: Props) {
  const { owner, bust, refresh } = useHousehold();
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  const showToast = (message: string, type: "success" | "error") => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 4000);
  };

  const handleSubmit = async (values: { name: string; email: string }) => {
    setSaving(true);
    try {
      const res = await updateProfile({
        name: values.name,
        email: values.email,
      });
      if (res.success) {
        showToast("Profile saved successfully", "success");
        onSaved({ name: values.name, email: values.email });
        // Re-read the roster so the topbar chip and the welcome card show the
        // new name immediately — the whole point of the owner's complaint.
        refresh();
      } else {
        showToast("Failed to save profile", "error");
      }
    } catch (err: any) {
      showToast(err.message || "Failed to save profile", "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col">
      {/* Toast */}
      {toast && (
        <div
          className={`mb-3 flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium ${
            toast.type === "success"
              ? "bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-400"
              : "bg-red-50 text-red-700 dark:bg-red-900/20 dark:text-red-400"
          }`}
        >
          <Icon
            path={toast.type === "success" ? mdiCheckCircle : mdiAlertCircle}
            size="16"
          />
          {toast.message}
        </div>
      )}

      {/* Avatar. The submit below sends only name and email, so the picture
          needs its own control. This one posts, and offers the pictures
          already on the appliance, which is the only route that works on the
          kiosk. */}
      <CardBox className="mb-6">
        <div
          className="mb-2"
          style={{
            font: "800 11px var(--font-nunito), sans-serif",
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: "var(--text2)",
          }}
        >
          Your picture
        </div>
        <AvatarPicker
          target="profile"
          name={owner?.display_name || profile.name || "Operator"}
          src={owner ? avatarSrc(owner, bust) : null}
          size={72}
          onChanged={refresh}
        />
      </CardBox>

      <CardBox className="flex-1" hasComponentLayout>
        <Formik
          initialValues={{
            name: profile.name || "",
            email: profile.email || "",
          }}
          enableReinitialize
          onSubmit={handleSubmit}
        >
          {({ isSubmitting: _isSubmitting }) => (
            <Form className="flex flex-1 flex-col">
              <CardBoxComponentBody>
                <FormField
                  label="Name"
                  help="Required. Your name"
                  labelFor="name"
                  icon={mdiAccount}
                >
                  {({ className }) => (
                    <Field name="name" id="name" placeholder="Name" className={className} />
                  )}
                </FormField>
                <FormField
                  label="E-mail"
                  help="Required. Your e-mail"
                  labelFor="email"
                  icon={mdiMail}
                >
                  {({ className }) => (
                    <Field name="email" id="email" placeholder="E-mail" className={className} />
                  )}
                </FormField>
              </CardBoxComponentBody>
              <CardBoxComponentFooter>
                <Buttons>
                  <Button
                    color="info"
                    type="submit"
                    label={saving ? "Saving..." : "Save Profile"}
                    icon={saving ? mdiLoading : mdiContentSave}
                    disabled={saving}
                    isGrouped
                  />
                </Buttons>
              </CardBoxComponentFooter>
            </Form>
          )}
        </Formik>
      </CardBox>
    </div>
  );
}
