"use client";

import { mdiCheckDecagram, mdiShieldCheck } from "@mdi/js";
import { Field, Form, Formik } from "formik";
import { useAppSelector } from "../../_stores/hooks";
import CardBox from ".";
import FormCheckRadio from "../FormField/CheckRadio";
import PillTag from "../PillTag";
import Icon from "../Icon";

type Props = {
  className?: string;
};

const CardBoxUser = ({ className }: Props) => {
  // One identity source: HouseholdSync pushes the household owner in here, so
  // this says the owner's name instead of the template's "PuffPAI User".
  // "Operator" is the pre-load fallback only.
  const userName = useAppSelector((state) => state.main.userName) || "Operator";
  const userAvatar = useAppSelector((state) => state.main.userAvatar);

  return (
    <CardBox className={className}>
      <div className="flex flex-col items-center justify-around lg:flex-row lg:justify-center">
        {/* PuffPAI-branded avatar */}
        <div className="mb-6 lg:mx-12 lg:mb-0">
          <div className="relative flex h-32 w-32 items-center justify-center rounded-full bg-gradient-to-br from-[#00B4D8] via-[#0096C7] to-[#005F8A] text-6xl shadow-lg ring-4 ring-white/50 dark:ring-slate-700/50">
            {userAvatar ? (
              /* eslint-disable-next-line @next/next/no-img-element */
              <img
                src={userAvatar}
                alt=""
                className="h-full w-full rounded-full object-cover"
              />
            ) : (
              "\u{1F916}"
            )}
            <div className="absolute -bottom-1 -right-1 flex h-8 w-8 items-center justify-center rounded-full bg-green-400 shadow-md ring-2 ring-white dark:ring-slate-800">
              <Icon path={mdiShieldCheck} size="16" className="text-white" />
            </div>
          </div>
        </div>
        <div className="space-y-3 text-center md:text-left lg:mx-12">
          <div className="flex justify-center md:block">
            <Formik
              initialValues={{
                notifications: ["1"],
              }}
              onSubmit={(values) => alert(JSON.stringify(values, null, 2))}
            >
              <Form>
                <FormCheckRadio type="switch" label="Notifications">
                  <Field type="checkbox" name="notifications" value={"1"} />
                </FormCheckRadio>
              </Form>
            </Formik>
          </div>
          <h1 className="text-2xl">
            Welcome, <b>{userName}</b>!
          </h1>
          <p className="text-gray-500 dark:text-slate-400">
            Privacy-first AI assistant, nothing leaves your device without your consent.
          </p>
          <div className="flex justify-center gap-2 md:block">
            <PillTag label="Local AI" color="info" icon={mdiCheckDecagram} />
            <PillTag label="Encrypted" color="success" icon={mdiShieldCheck} />
          </div>
        </div>
      </div>
    </CardBox>
  );
};

export default CardBoxUser;
