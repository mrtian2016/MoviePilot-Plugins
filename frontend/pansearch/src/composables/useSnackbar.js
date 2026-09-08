import {ref} from "vue";

const snackbarVisible = ref(false);
const snackbarText = ref("");
const snackbarColor = ref("success");

export function useSnackbar() {
  function showMessage(msg, color = "success") {
    snackbarText.value = msg;
    snackbarColor.value = color;
    snackbarVisible.value = true;
  }

  return {
    snackbarVisible,
    snackbarText,
    snackbarColor,
    showMessage,
  };
}
